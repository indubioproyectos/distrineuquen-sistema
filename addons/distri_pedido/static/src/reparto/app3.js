/* Delivery driver app (offline-first PWA).
 *
 * 19.0.1.115.0: app3.js (app2.js stays for the cached 19.0.1.114.0 page):
 * amounts in the Argentine format without Intl (money, moneyRaw,
 * parseMoney) and a route reopened by administración opens again here.
 *
 * - The route is downloaded once (connection at the depot) and kept in IndexedDB.
 * - Every change is saved on the phone first and queued in an outbox as an
 *   event with a UUID generated here. The outbox is sent on app open, when
 *   the connection comes back and every 30 s while the app is open. The
 *   server answers "duplicate" for an event it already has, so a retry never
 *   duplicates anything.
 * - The receipt code is checked offline against a salted SHA-256 hash: the
 *   downloaded route never contains the code itself.
 * - Photos (expense receipts, parcel guides) are compressed on the phone and
 *   travel inside their outbox event, so they are kept in IndexedDB until the
 *   server has them. Only a small thumbnail stays in the local state.
 * - The transfer receipt photographed at the visit (19.0.1.86.0,
 *   models/comprobante_app.py) travels in its own «receipt» event, queued
 *   right after the visit, with an id of the photo (photo_uuid): the visit
 *   stays light and the server never creates the same photo twice.
 * - Location (19.0.1.35.0, models/reparto_gps.py): one fresh position only at
 *   «Salir a repartir», «Llegué», closing a visit and «Cerrar la ruta», and
 *   only while the route is active. It travels inside the event as "gps".
 *   Never a watch, never in the background.
 * - 19.0.1.113.0 (this file replaces app.js, which stays one version for the
 *   19.0.1.112.0 template: static files are served live):
 *   · the phone's state is kept per user and per route. IndexedDB «reparto»
 *     holds "uid" (the last user seen) and, for each user, "u:<uid>:cur"
 *     (the route on screen), "u:<uid>:route:<id>" and "u:<uid>:local:<id>".
 *     Every outbox event carries its "uid": another user's events stay on the
 *     phone, under their user, and are never sent as the user logged in now
 *     (the server also refuses a batch whose "user_id" is not the session's).
 *   · two routes a day: «Tus rutas de hoy» lists the driver's open routes
 *     (the one «En reparto» first) and opens any of them without settling
 *     the other; each keeps its own visits and its own pending changes.
 *   · a route that is gone (cancelled, given to another driver, settled): the
 *     server answers with a "reset" flag and the current route. Its pending
 *     changes are kept and still sent: the server applies them or keeps them
 *     in «Cambios rechazados» (distri.sync.rejection), as before.
 * - 19.0.1.129.22 («etapa 0»):
 *   · a visit rejected by the server and then resolved or resent there takes
 *     the server's visit on download (reconcileVisits); only a rejection
 *     still open on the server blocks «Cerrar la ruta», which asks the
 *     server first;
 *   · «Ya estaba pagado» when the server says the order is paid
 *     (stop.payments_paid): delivered, no cash, no transfer, payload flag
 *     already_paid; the server may answer «ok» with a warning (still owed);
 *   · «Entregar igual» sets (one tap), «deshacer» clears it, and before
 *     «¿Devolvió algo?» it says so instead of doing nothing.
 * - 19.0.1.129.25:
 *   · «Ya estaba pagado · a confirmar» when the customer uploaded a receipt
 *     not credited yet that covers what is due (stop.payments_to_confirm):
 *     the same already_paid visit plus to_confirm; nothing at the driver's
 *     name, administración confirms it with Mercado Pago;
 *   · «Reabrir la ruta» on «Ruta cerrada» (POST /reparto/api/reopen, online
 *     only; the server refuses it once the cash was counted), and a route
 *     reopened by administración opens again from the poll (onRouteState).
 * - 19.0.1.129.28 («Reparto v2», app5.js, only when the route carries
 *   reparto_v2): guarded hooks for app5.js (extHook "view" in render, a
 *   "homeHtml" card on the home screens, "visitsTools" on the visits list,
 *   "routeApplied" after each download) and more of this file in
 *   window.RepartoApp. Without app5.js, or with the flag off, nothing
 *   changes: every hook returns nothing and the screens are the same.
 *   app3.js and app4.js are edited in place: asset_version()
 *   (controllers/reparto.py) hashes their size and mtime, so the page and
 *   the service worker pick up the new files.
 * - 19.0.1.129.29 (#100): «‹ Atrás» goes UP one level instead of adding a
 *   history entry. Every in-page level remembers the level it was opened
 *   from (history.state.dnFrom, set on hashchange); a back link (a.back)
 *   to that level is history.back(), to any other level it replaces the
 *   entry; go() back to the level it came from is history.back() too. The
 *   markup is unchanged (the same links), so app5.js's back links follow
 *   the same rule. Without history (old browsers, the Node harness) the
 *   links work as before.
 * - 19.0.1.129.33 («Etapa 3b»): one more guarded hook, "customerHtml" on
 *   «Ubicación y horarios» (app5.js draws the customer's WhatsApp there,
 *   «Reparto v2» only), and saveWhatsapp / canReopen in window.RepartoApp.
 *   Without app5.js or with the flag off the screen is the same.
 */
(function () {
  "use strict";

  var API_ROUTE = "/reparto/api/route";
  var API_SYNC = "/reparto/api/sync";
  // both ticks of each order while loading (the dispatcher checks on /despacho)
  var API_LOAD = "/reparto/api/load";
  // 19.0.1.129.25: «Reabrir la ruta» (controllers/reparto.py)
  var API_REOPEN = "/reparto/api/reopen";
  // 19.0.1.92.0: «Ver cuenta» of a stop (online only) and the cash page
  var API_ACCOUNT = "/reparto/api/account";
  var CASH_PAGE = "/cobro";
  // every customer's account, for drivers too (controllers/cuentas.py)
  var ACCOUNTS_APP = "/cuentas";
  // 19.0.1.122.0: the driver's own routes of the last days, read only
  // (online pages, controllers/reparto_anteriores.py)
  var PAST_ROUTES = "/reparto/anteriores";
  var SYNC_EVERY_MS = 30000;
  var LOAD_POLL_MS = 5000;
  // one POST stays under the server limit (controllers/reparto.py MAX_BODY)
  var BATCH_MAX_CHARS = 1500000;
  var PHOTO_MAX_SIDE = 1600, PHOTO_MAX_CHARS = 900000, THUMB_SIDE = 220;
  var EXPENSE_KINDS = [["fuel", "Nafta"], ["parts", "Repuestos"], ["parking", "Estacionamiento"],
                       ["parcel", "Encomienda"], ["other", "Otro"]];
  var PHOTO_WARNING = "La foto tiene que mostrar todos los datos del comprobante, legibles. " +
    "El comprobante en papel se entrega en la rendición.";
  // the transfer receipt photographed at the visit (comprobante_app.py)
  var RECEIPT_PHOTO_WARNING = "La foto tiene que mostrar todos los datos del comprobante, legibles: " +
    "importe, fecha y hora, número de operación y quién transfirió.";
  // no «capture»: the phone offers the camera and the gallery
  // 19.0.1.99.0 (audit F16): a PDF too (many banks give the receipt as PDF)
  var RECEIPT_INPUT = '<input type="file" accept="image/*,application/pdf,.pdf" data-f="rphoto" hidden>';
  var PDF_MAX_BYTES = 3 * 1024 * 1024;
  var PDF_THUMB = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" width="160" height="200" viewBox="0 0 160 200">' +
    '<rect width="160" height="200" rx="12" fill="#eef2f6"/><path d="M50 40h45l20 20v100H50z" fill="#fff" stroke="#10243f" stroke-width="5"/>' +
    '<text x="80" y="128" font-family="sans-serif" font-size="30" font-weight="700" text-anchor="middle" fill="#10243f">PDF</text></svg>');
  var RECEIPT_LABELS = { forwarded: "reenviado", customer_wa: "lo manda el cliente", not_yet: "pendiente" };
  var REASONS = ["Envase golpeado", "Vencido", "No lo pidió", "Producto equivocado", "Sobrante"];
  var DAYS = ["Lu", "Ma", "Mi", "Ju", "Vi", "Sá", "Do"];

  // ------------------------------------------------------------ helpers

  function h(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  // 19.0.1.115.0 (app3.js): the Argentine format by hand, «.» for thousands
  // and «,» for cents: a phone without the es-AR locale data showed
  // «14,307.6», which parseMoney then read as 14,31
  function money(n) {
    var v = Math.round((Number(n) || 0) * 100) / 100, neg = v < 0;
    v = Math.abs(v);
    var whole = Math.floor(v), cents = Math.round((v - whole) * 100);
    if (cents === 100) { whole += 1; cents = 0; }
    var text = String(whole).replace(/\B(?=(\d{3})+(?!\d))/g, ".");
    if (cents) text += "," + (cents < 10 ? "0" : "") + cents;
    return "$ " + (neg ? "-" : "") + text;
  }
  // the amount to edit: no «$», no thousands dots, «,» for cents
  function moneyRaw(n) {
    var v = round2(n);
    return v ? String(v).replace(".", ",") : "";
  }
  function qty(n) { return String(Math.round((Number(n) || 0) * 1000) / 1000).replace(".", ","); }
  function round2(n) { return Math.round((Number(n) || 0) * 100) / 100; }

  // 19.0.1.115.0: pesos with optional cents. A «,» or «.» followed by one or
  // two digits at the end is the cents separator, any other «.» or «,» is a
  // thousands separator: «14.307,60», «14.307.60», «14307.6» and «14,307.60»
  // are all 14307,6; «25.000» and «25,000» are 25000; «25.00» is 25. An
  // amount is never divided by a thousand.
  function parseMoney(text) {
    var s = String(text == null ? "" : text).replace(/[^\d.,]/g, "");
    if (!s) return 0;
    var m = /^(.*?)[.,](\d{1,2})$/.exec(s);
    var whole = m ? m[1] : s, cents = m ? m[2] : "";
    whole = whole.replace(/[.,]/g, "");
    if (!whole && !cents) return NaN;
    var n = parseFloat((whole || "0") + (cents ? "." + cents : ""));
    return isFinite(n) && n >= 0 ? round2(n) : NaN;
  }

  function uuid() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    var b = new Uint8Array(16);
    crypto.getRandomValues(b);
    b[6] = (b[6] & 15) | 64; b[8] = (b[8] & 63) | 128;
    var x = Array.prototype.map.call(b, function (v) { return (v + 256).toString(16).slice(1); }).join("");
    return x.slice(0, 8) + "-" + x.slice(8, 12) + "-" + x.slice(12, 16) + "-" + x.slice(16, 20) + "-" + x.slice(20);
  }

  var WEEKDAYS = ["domingo", "lunes", "martes", "miércoles", "jueves", "viernes", "sábado"];
  function dateLabel(iso) {
    if (!iso) return "";
    var p = iso.split("-");
    var d = new Date(+p[0], +p[1] - 1, +p[2]);
    return WEEKDAYS[d.getDay()] + " " + p[2] + "/" + p[1];
  }

  // SHA-256: WebCrypto when available (https), else a small fallback.
  function sha256Hex(text) {
    var bytes = new TextEncoder().encode(text);
    if (window.crypto && crypto.subtle) {
      return crypto.subtle.digest("SHA-256", bytes).then(function (buf) {
        return Array.prototype.map.call(new Uint8Array(buf), function (v) {
          return (v + 256).toString(16).slice(1);
        }).join("");
      });
    }
    return Promise.resolve(sha256Fallback(bytes));
  }

  function sha256Fallback(msg) {
    var K = [], H = [], i, j;
    function frac(x) { return ((x - Math.floor(x)) * 4294967296) | 0; }
    for (var n = 2, c = 0; c < 64; n++) {
      var prime = true;
      for (i = 2; i * i <= n; i++) if (n % i === 0) { prime = false; break; }
      if (prime) { if (c < 8) H[c] = frac(Math.pow(n, 1 / 2)); K[c] = frac(Math.pow(n, 1 / 3)); c++; }
    }
    var len = msg.length, words = [], total = ((len + 9 + 63) >> 6) << 4;
    for (i = 0; i < total; i++) words[i] = 0;
    for (i = 0; i < len; i++) words[i >> 2] |= msg[i] << (24 - (i % 4) * 8);
    words[len >> 2] |= 0x80 << (24 - (len % 4) * 8);
    words[total - 1] = len * 8;
    function rotr(x, s) { return (x >>> s) | (x << (32 - s)); }
    var W = new Array(64);
    for (i = 0; i < total; i += 16) {
      var a = H[0], b = H[1], cc = H[2], d = H[3], e = H[4], f = H[5], g = H[6], hh = H[7];
      for (j = 0; j < 64; j++) {
        if (j < 16) W[j] = words[i + j] | 0;
        else {
          var s0 = rotr(W[j - 15], 7) ^ rotr(W[j - 15], 18) ^ (W[j - 15] >>> 3);
          var s1 = rotr(W[j - 2], 17) ^ rotr(W[j - 2], 19) ^ (W[j - 2] >>> 10);
          W[j] = (W[j - 16] + s0 + W[j - 7] + s1) | 0;
        }
        var t1 = (hh + (rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25)) + ((e & f) ^ (~e & g)) + K[j] + W[j]) | 0;
        var t2 = ((rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22)) + ((a & b) ^ (a & cc) ^ (b & cc))) | 0;
        hh = g; g = f; f = e; e = (d + t1) | 0; d = cc; cc = b; b = a; a = (t1 + t2) | 0;
      }
      H[0] = (H[0] + a) | 0; H[1] = (H[1] + b) | 0; H[2] = (H[2] + cc) | 0; H[3] = (H[3] + d) | 0;
      H[4] = (H[4] + e) | 0; H[5] = (H[5] + f) | 0; H[6] = (H[6] + g) | 0; H[7] = (H[7] + hh) | 0;
    }
    return H.map(function (v) { return ("00000000" + (v >>> 0).toString(16)).slice(-8); }).join("");
  }

  // ----------------------------------------------------------- IndexedDB

  var dbPromise = null;
  function db() {
    if (!dbPromise) {
      dbPromise = new Promise(function (resolve, reject) {
        var req = indexedDB.open("reparto", 1);
        req.onupgradeneeded = function () {
          var d = req.result;
          if (!d.objectStoreNames.contains("kv")) d.createObjectStore("kv");
          if (!d.objectStoreNames.contains("outbox")) d.createObjectStore("outbox", { keyPath: "uuid" });
        };
        req.onsuccess = function () { resolve(req.result); };
        req.onerror = function () { reject(req.error); };
      });
    }
    return dbPromise;
  }
  function tx(store, mode, fn) {
    return db().then(function (d) {
      return new Promise(function (resolve, reject) {
        var t = d.transaction(store, mode);
        var result = fn(t.objectStore(store));
        t.oncomplete = function () { resolve(result && "result" in result ? result.result : result); };
        t.onerror = function () { reject(t.error); };
        t.onabort = function () { reject(t.error); };
      });
    });
  }
  var kvGet = function (key) { return tx("kv", "readonly", function (s) { return s.get(key); }); };
  var kvSet = function (key, value) { return tx("kv", "readwrite", function (s) { s.put(value, key); }); };
  var outboxAll = function () { return tx("outbox", "readonly", function (s) { return s.getAll(); }); };
  var outboxPut = function (ev) { return tx("outbox", "readwrite", function (s) { s.put(ev); }); };
  var outboxDel = function (id) { return tx("outbox", "readwrite", function (s) { s.delete(id); }); };
  var kvDel = function (key) { return tx("kv", "readwrite", function (s) { s.delete(key); }); };
  var kvKeys = function () { return tx("kv", "readonly", function (s) { return s.getAllKeys(); }); };

  // --------------------------------------------------------------- state

  var S = {
    route: null,      // as downloaded
    local: null,      // what the driver did on this phone
    outbox: [],
    online: navigator.onLine,
    auth: true,
    syncing: false,
    otherRoutes: [],
    user: "",
    departing: false,  // «Salir a repartir» on its way to the server
    departError: null, // why the server did not let the route leave
    uid: null,         // the user this state belongs to (null: not known yet)
    locals: {},        // route id -> local state, for every route of this user
    gone: null,        // {id, name}: a route that is gone, with changes on the phone
  };
  var draft = null;   // visit being edited
  var custDraft = null;
  var expDraft = null; // expense being loaded

  function emptyLocal(routeId) {
    return { routeId: routeId, visits: {}, loaded: {}, departed: false, closed: false,
             customers: {}, whatsapp: {}, rejected: [], expenses: {}, arrivals: {} };
  }
  // 19.0.1.113.0: every key is under its user (see the comment at the top)
  function userKey(name, routeId) {
    return "u:" + S.uid + ":" + name + (routeId !== undefined ? ":" + routeId : "");
  }
  function saveLocal() {
    if (!S.local) return Promise.resolve();
    if (S.local.routeId) S.locals[S.local.routeId] = S.local;
    if (S.uid === null) return kvSet("local", S.local); // before the first download
    return S.local.routeId ? kvSet(userKey("local", S.local.routeId), S.local) : Promise.resolve();
  }
  function saveLocalOf(local) {
    if (local === S.local) return saveLocal();
    return S.uid === null ? Promise.resolve() : kvSet(userKey("local", local.routeId), local);
  }
  function saveRoute() {
    if (S.uid === null) return kvSet("route", S.route);
    if (!S.route) return kvSet(userKey("cur"), null);
    return kvSet(userKey("route", S.route.id), S.route).then(function () {
      return kvSet(userKey("cur"), S.route.id);
    });
  }
  // the local state of the route an event belongs to (it may not be the one
  // on screen: each route keeps its own changes)
  function localFor(routeId) {
    if (S.local && S.local.routeId === routeId) return S.local;
    if (!S.locals[routeId]) S.locals[routeId] = emptyLocal(routeId);
    return S.locals[routeId];
  }
  function fillLocal(local) {
    local.rejected = local.rejected || [];
    local.expenses = local.expenses || {};
    local.arrivals = local.arrivals || {};
    return local;
  }
  function stops() { return S.route ? S.route.stops : []; }
  function stopById(id) { return stops().filter(function (s) { return s.id === +id; })[0]; }
  function visitOf(stop) { return S.local.visits[stop.id] || null; }

  function stopState(stop) {
    var v = visitOf(stop);
    if (!v) return "pending";
    if (v.outcome === "not_delivered") return "not_delivered";
    if (v.outcome === "dispatched") return "dispatched";
    // 19.0.1.129.22: «Ya estaba pagado» (models/reparto_sync.py)
    if (v.already_paid) return "already_paid";
    if (v.no_payment) return "not_paid";
    if (v.cash > 0 || v.transfer === "yes" || v.transfer === "done") return "paid";
    return "not_paid";
  }
  var PILL = { pending: "PENDIENTE", paid: "COBRADO", not_paid: "NO PAGÓ", already_paid: "YA ESTABA PAGADO",
               not_delivered: "NO SE ENTREGÓ", dispatched: "DESPACHADA" };
  // 19.0.1.129.25: «Ya estaba pagado · a confirmar» (models/reparto_a_confirmar.py)
  var TO_CONFIRM_LABEL = "Ya estaba pagado · a confirmar";

  // --- expenses paid with the day's cash

  function kindLabel(kind) {
    var k = EXPENSE_KINDS.filter(function (x) { return x[0] === kind; })[0];
    return k ? k[1] : "Gasto";
  }
  // the driver's own expenses plus the shipping of the parcels Distrineuquén pays
  function expenseItems() {
    var items = Object.keys(S.local.expenses || {}).map(function (k) { return S.local.expenses[k]; })
      .filter(function (e) { return !e.deleted; })
      .sort(function (a, b) { return a.captured_at < b.captured_at ? -1 : 1; })
      .map(function (e) {
        return { uuid: e.uuid, kind: e.kind, amount: e.amount, comment: e.comment, thumb: e.thumb,
                 synced: e.synced, error: e.error, own: true };
      });
    stops().forEach(function (s) {
      var v = visitOf(s);
      if (s.parcel && v && v.outcome === "dispatched" && v.cost > 0) {
        items.push({ kind: "parcel", amount: v.cost, comment: "Envío " + s.order + " · guía " +
          (v.guide_number || "-"), thumb: v.thumb, synced: v.synced, error: v.error, own: false });
      }
    });
    return items;
  }
  function expensesTotal() {
    return round2(expenseItems().reduce(function (sum, e) { return sum + (e.amount || 0); }, 0));
  }

  // --- photos: compressed on the phone before they are stored and sent

  function loadImage(file) {
    return new Promise(function (resolve, reject) {
      var url = URL.createObjectURL(file);
      var img = new Image();
      img.onload = function () { URL.revokeObjectURL(url); resolve(img); };
      img.onerror = function () { URL.revokeObjectURL(url); reject(new Error("No se pudo leer la foto.")); };
      img.src = url;
    });
  }
  function drawScaled(img, maxSide, quality) {
    var w = img.naturalWidth || img.width, hgt = img.naturalHeight || img.height;
    var scale = Math.min(1, maxSide / Math.max(w, hgt));
    var c = document.createElement("canvas");
    c.width = Math.max(1, Math.round(w * scale)); c.height = Math.max(1, Math.round(hgt * scale));
    var ctx = c.getContext("2d");
    ctx.fillStyle = "#fff"; ctx.fillRect(0, 0, c.width, c.height);
    ctx.drawImage(img, 0, 0, c.width, c.height);
    return c.toDataURL("image/jpeg", quality);
  }
  function readPhoto(file) {
    return loadImage(file).then(function (img) {
      var data = drawScaled(img, PHOTO_MAX_SIDE, 0.72);
      if (data.length > PHOTO_MAX_CHARS) data = drawScaled(img, 1280, 0.6);
      if (data.length > PHOTO_MAX_CHARS) data = drawScaled(img, 1024, 0.5);
      return { photo: data, thumb: drawScaled(img, THUMB_SIDE, 0.6) };
    });
  }
  // the transfer receipt: a photo (compressed) or a PDF (as is)
  function isPdf(file) { return file.type === "application/pdf" || /\.pdf$/i.test(file.name || ""); }
  function readReceipt(file) {
    if (!isPdf(file)) return readPhoto(file);
    if (file.size > PDF_MAX_BYTES) return Promise.reject(new Error("El PDF es demasiado grande (más de 3 MB). Sacale una foto a la pantalla."));
    return new Promise(function (resolve, reject) {
      var reader = new FileReader();
      reader.onload = function () { resolve({ photo: reader.result, thumb: PDF_THUMB, pdf: true }); };
      reader.onerror = function () { reject(new Error("No se pudo leer el PDF.")); };
      reader.readAsDataURL(file);
    });
  }
  function photoField(d, label) {
    if (d.thumb) {
      return '<div class="photo"><img src="' + d.thumb + '" alt="Foto"><label class="opt second">' +
        'Sacar otra foto<input type="file" accept="image/*" capture="environment" data-f="photo" hidden></label></div>';
    }
    return '<label class="btn photo-btn">' + ICON_CAMERA + h(label) +
      '<input type="file" accept="image/*" capture="environment" data-f="photo" hidden></label>';
  }

  function customerOf(stop) {
    var local = S.local.customers[stop.shipping_partner_id];
    return local || stop.customer || {};
  }
  function whatsappOf(stop) {
    var local = S.local.whatsapp[stop.partner_id];
    return local || { number: stop.whatsapp || "", confirmed: !!stop.whatsapp_confirmed };
  }

  function returnsAmount(stop, returns) {
    return round2((returns || []).reduce(function (sum, r) {
      var line = stop.lines.filter(function (l) { return l.id === r.line_id; })[0];
      return sum + (line ? line.unit_price * (Number(r.qty) || 0) : 0);
    }, 0));
  }
  function balance(stop, returns) {
    return round2(stop.order_amount + stop.previous_debt - returnsAmount(stop, returns));
  }

  // «Cliente en rojo» (models/comprobante_pendiente.py): only while the
  // setting is on, the customer owes money or receipts from the days before
  function redOf(stop) { return S.route && S.route.red_rule && stop.red ? stop.red : null; }
  function dueToday(stop, returns) {
    return Math.max(round2((stop.order_amount || 0) - returnsAmount(stop, returns)), 0);
  }
  // what the visit has to collect: today's order, plus the old debt when red
  function toCollect(stop, returns) {
    var red = redOf(stop);
    return round2(dueToday(stop, returns) + (red ? red.debt || 0 : 0));
  }
  // same rule as wa_digits in models/pedido_whatsapp.py: 54 9 + area
  // without 0 + number without 15; the 15 is dropped only when unambiguous
  function arNational(number) {
    var d = String(number || "").replace(/\D/g, "").replace(/^0+/, "");
    if (d.indexOf("54") === 0 && d.length >= 12) {
      d = d.slice(2);
      if (d.charAt(0) === "9" && (d.length === 11 || d.length === 13)) d = d.slice(1);
      d = d.replace(/^0+/, "");
    }
    if (d.length === 10) return d;
    if (d.length === 12) {
      var cuts = [2, 3, 4].filter(function (n) { return d.substr(n, 2) === "15"; });
      if (cuts.length === 1) return d.slice(0, cuts[0]) + d.slice(cuts[0] + 2);
    }
    return "";
  }
  function waDigits(number) {
    var text = String(number || "").trim();
    var d = text.replace(/\D/g, "").replace(/^0+/, "");
    if (text.charAt(0) === "+" && d.indexOf("54") !== 0) return d;
    var national = arNational(text);
    if (national) return "549" + national;
    if (d && d.indexOf("54") !== 0) d = "549" + d;
    return d;
  }
  function waLink(digits, text) {
    return "https://wa.me/" + digits + (text ? "?text=" + encodeURIComponent(text) : "");
  }
  // --- what the driver claims: claim.js applies the wording the server sends
  // with the route (static/src/reparto/claim_texts.json), the same Odoo uses
  var claimTool = null, claimTexts = null;
  function claims() {
    var T = S.route && S.route.claim_texts;
    if (!T || !window.DistriClaim) return null;
    if (T !== claimTexts) { claimTexts = T; claimTool = window.DistriClaim(T); }
    return claimTool;
  }
  function relWhen(value, nowMs) { var c = claims(); return c ? c.relWhen(value, nowMs) : ""; }
  function itemLine(r) {
    var c = claims();
    return c ? c.itemLine(r) : money(r.amount) + " · " + (r.document || r.order);
  }
  function kindLabel2(kind) { var c = claims(); return c ? c.kindLabel(kind) : ""; }
  function itemHtml(r) {
    return '<div class="owe"><b>' + h(kindLabel2(r.kind)) + "</b><span>" + h(itemLine(r)) + "</span></div>";
  }
  // one WhatsApp per customer: every item, oldest first, with the total
  function customerClaim(items) { var c = claims(); return c ? c.customerClaim(items, "driver") : ""; }
  function customerItems(partnerId) {
    return receiptItems().filter(function (r) { return r.partner_id === partnerId && r.state !== "fulfilled"; });
  }
  // the claim of a visit being saved: this transfer plus what the customer already owes
  function visitClaim(stop, amount) {
    var others = customerItems(stop.commercial_partner_id).filter(function (r) { return r.stop_id !== stop.id; });
    return customerClaim(others.concat([{ stop_id: stop.id, order: stop.order, document: stop.document, kind: "done",
      amount: amount, state: "open", delivered_at: new Date().toISOString() }]));
  }

  function daysHoursText(c) {
    var names = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"];
    var days = String(c.days || "").split(",").filter(function (d) { return d !== ""; }).map(Number);
    var dtext = "";
    if (days.length > 2 && days[days.length - 1] - days[0] === days.length - 1) {
      dtext = names[days[0]] + " a " + names[days[days.length - 1]];
    } else {
      dtext = days.map(function (d) { return names[d]; }).join(", ");
    }
    var htext = String(c.hours || "").split(";").filter(Boolean).map(function (r) {
      return r.split("-").map(function (t) { return t.replace(/^0(\d)/, "$1"); }).join(" a ");
    }).join(" y ");
    var text = [dtext, htext].filter(Boolean).join(", ");
    return text ? "Recibe " + text : "";
  }

  // «Aviso del cliente»: what the customer left for this order from its
  // link. It comes with the route: a new one shows after «Actualizar la ruta».
  function noticeHtml(stop, extra) {
    var n = stop.notice;
    var cls = '<div class="notice' + (extra ? " " + extra : "") + '">';
    var out = "";
    // 19.0.1.93.0: held by administración (C4) and cash already paid at the local (C1)
    if (stop.retained) {
      out += cls + "<b>Retenido:</b> no lo entregues ni lo cobres. Marcalo «No se entregó» y " +
        "volvelo al depósito. Motivo: " + h(stop.retained) + "</div>";
    }
    if (stop.paid_elsewhere > 0) {
      out += cls + "<b>Ya cobrado en el local: " + h(money(stop.paid_elsewhere)) + ".</b> " +
        "El saldo de abajo ya lo descuenta: no lo cobres de nuevo.</div>";
    }
    // 19.0.1.129.16: «Pagado / comprobante cargado» (app4.js, loaded after
    // this file; without it nothing changes)
    out += extHook("noticeHtml", stop, extra) || "";
    // 19.0.1.98.0: the order's depot note and the customer's comment
    if (stop.depot_note) out += cls + "<b>Nota del pedido:</b> " + h(stop.depot_note) + "</div>";
    if (stop.comment) out += cls + "<b>Comentario del cliente:</b> " + h(stop.comment) + "</div>";
    if (!n || !n.text) return out;
    return out + cls + "<b>Aviso del cliente:</b> " + h(n.text) +
      (n.date ? ' <span class="small">(' + h(shortDate(n.date)) + ")</span>" : "") + "</div>";
  }

  // 19.0.1.129.16: the extension loaded after this file (app4.js) sets
  // window.RepartoExt; a missing or failing extension never breaks the app
  function extHook(name) {
    var ext = window.RepartoExt;
    if (!ext || typeof ext[name] !== "function") return null;
    try {
      return ext[name].apply(null, Array.prototype.slice.call(arguments, 1));
    } catch (err) {
      return null;
    }
  }

  // ------------------------------------------------------------- outbox

  // «Cambios rechazados» (19.0.1.29.0): what the server rejected stays on the
  // phone, whole, until it is resent or administración resolves it
  function openRejections() { return (S.local.rejected || []).filter(function (x) { return x.event; }); }
  function rejectionFor(key) {
    return key ? openRejections().filter(function (x) { return x.event.key === key; })[0] : null;
  }

  function enqueue(type, payload, replaceKey, resends) {
    var ev = {
      uuid: uuid(), type: type, route_id: S.route.id, uid: S.uid,
      captured_at: new Date().toISOString(), payload: payload, key: replaceKey || null,
    };
    // a corrected change names the rejected one: the server closes it and
    // never counts it twice
    var rej = resends ? null : rejectionFor(replaceKey);
    if (resends || rej) ev.resends = resends || rej.root;
    var stale = replaceKey ? S.outbox.filter(function (e) { return e.key === replaceKey; }) : [];
    S.outbox = S.outbox.filter(function (e) { return stale.indexOf(e) < 0; }).concat([ev]);
    return Promise.all(stale.map(function (e) { return outboxDel(e.uuid); }))
      .then(function () { return outboxPut(ev); })
      .then(function () { syncNow(); });
  }

  // 19.0.1.129.28: an event of another route of this driver (a receipt photo
  // from «Mis comprobantes a reclamar», app5.js): the same outbox
  function enqueueRoute(routeId, type, payload, replaceKey) {
    var ev = {
      uuid: uuid(), type: type, route_id: routeId, uid: S.uid,
      captured_at: new Date().toISOString(), payload: payload, key: replaceKey || null,
    };
    var stale = replaceKey ? S.outbox.filter(function (e) { return e.key === replaceKey; }) : [];
    S.outbox = S.outbox.filter(function (e) { return stale.indexOf(e) < 0; }).concat([ev]);
    return Promise.all(stale.map(function (e) { return outboxDel(e.uuid); }))
      .then(function () { return outboxPut(ev); })
      .then(function () { syncNow(); });
  }

  // a change that never reached the server and is no longer wanted
  function dropQueued(key) {
    var queued = S.outbox.filter(function (e) { return e.key === key; });
    if (!queued.length) return Promise.resolve();
    S.outbox = S.outbox.filter(function (e) { return queued.indexOf(e) < 0; });
    return Promise.all(queued.map(function (e) { return outboxDel(e.uuid); }));
  }

  function isLoginPage(res) {
    var type = res.headers.get("Content-Type") || "";
    return res.redirected || type.indexOf("application/json") < 0;
  }

  // One pass at a time; a call during a pass queues one more pass.
  var running = null, queued = null;
  function syncNow() {
    if (!running) {
      running = syncOnce().then(function () { running = null; });
      return running;
    }
    if (!queued) {
      queued = running.then(function () { queued = null; return syncNow(); });
    }
    return queued;
  }

  function fetchWithTimeout(url, options, ms) {
    if (!window.AbortController) return fetch(url, options);
    var ctrl = new AbortController();
    var timer = setTimeout(function () { ctrl.abort(); }, ms);
    options.signal = ctrl.signal;
    return fetch(url, options).then(function (res) { clearTimeout(timer); return res; },
      function (err) { clearTimeout(timer); throw err; });
  }

  function syncOnce() {
    // 19.0.1.113.0: nothing leaves the phone until the server said who is
    // logged in (download): the events of another user are never sent
    if (!S.outbox.length || S.uid === null) return Promise.resolve();
    S.syncing = true;
    // up to 100 events and ~1,5 MB per request: photos go a few at a time
    var batch = [], size = 0;
    for (var i = 0; i < S.outbox.length && batch.length < 100; i++) {
      var n = JSON.stringify(S.outbox[i].payload || {}).length + 200;
      if (batch.length && size + n > BATCH_MAX_CHARS) break;
      batch.push(S.outbox[i]); size += n;
    }
    var sent = batch.map(function (e) {
      var out = { uuid: e.uuid, type: e.type, route_id: e.route_id, captured_at: e.captured_at, payload: e.payload };
      if (e.resends) out.resends = e.resends;
      return out;
    });
    return fetchWithTimeout(API_SYNC, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ events: sent, user_id: S.uid }),
    }, 25000).then(function (res) {
      if (res.status === 409) {
        // another user logged in on this phone: download() switches to them
        // and keeps these events under their own user
        throw new Error("user");
      }
      if (isLoginPage(res) || res.status === 401 || res.status === 403) {
        S.auth = !isLoginPage(res) ? S.auth : false;
        throw new Error("auth");
      }
      return res.json();
    }).then(function (data) {
      S.online = true; S.auth = true;
      var byUuid = {}, touched = [];
      batch.forEach(function (e) { byUuid[e.uuid] = e; });
      var jobs = [];
      (data.results || []).forEach(function (r) {
        var ev = byUuid[r.uuid];
        if (!ev) return;
        // «discarded» (19.0.1.93.0): an edit older than the server's one, not
        // applied; the visit shows why and the driver saves it again
        if (r.status === "ok" || r.status === "duplicate" || r.status === "discarded" ||
            (r.status === "error" && !r.retry)) {
          S.outbox = S.outbox.filter(function (e) { return e.uuid !== r.uuid; });
          jobs.push(outboxDel(r.uuid));
          // the route the event belongs to, on screen or not
          var local = fillLocal(localFor(ev.route_id));
          if (touched.indexOf(local) < 0) touched.push(local);
          markVisit(ev, r.status === "error" || r.status === "discarded" ? r.error : null, local);
          // 19.0.1.129.22: «Ya estaba pagado» and the order still owes: the
          // server recorded it as not paid and says why
          if (r.status === "ok" && r.warning) noteWarning(ev, r.warning, local);
          if (r.status === "error") {
            keepRejected(ev, r.error, local);
          } else if (ev.resends) {
            local.rejected = (local.rejected || []).filter(function (x) { return x.root !== ev.resends; });
          }
        }
      });
      return Promise.all(jobs).then(function () {
        return Promise.all(touched.map(saveLocalOf));
      });
    }).catch(function (err) {
      if (err && err.message === "user") { S.online = true; return download(false); }
      if (!err || err.message !== "auth") S.online = false;
    }).then(function () {
      S.syncing = false;
      paintStatus();
    });
  }

  function keepRejected(ev, error, local) {
    local = local || S.local;
    var root = ev.resends || ev.uuid;
    var item = (local.rejected || []).filter(function (x) { return x.root === root; })[0];
    var copy = JSON.parse(JSON.stringify(ev));
    if (item) {
      item.event = copy; item.error = error; item.at = new Date().toISOString(); item.hidden = false;
    } else {
      local.rejected.push({ root: root, type: ev.type, event: copy, error: error,
                            at: new Date().toISOString(), hidden: false });
    }
  }

  function noteWarning(ev, warning, local) {
    var v = ev.type === "visit" ? local.visits[ev.payload.stop_id] : null;
    if (v && v.visit_uuid === ev.payload.visit_uuid && v.already_paid) {
      v.already_paid = false; v.to_confirm = false; v.no_payment = true; v.warning = warning;
    }
    toast(warning, true);
  }

  // 19.0.1.129.22: what the server says about the rejection of a visit with
  // an error on this phone: "open" (still blocks), "closed" (resent or
  // resolved by administración), "none" (the server has no rejection for
  // it: an edit «discarded» as older than the server's) or "unknown" (the
  // phone kept a rejection the server no longer lists)
  function visitRejection(local, rejections, stop, v) {
    var roots = (local.rejected || []).filter(function (x) {
      return x.event && (x.type === "visit" || x.type === "parcel") && x.event.payload &&
        +x.event.payload.stop_id === stop.id;
    }).map(function (x) { return x.root; });
    var known = false, open = false;
    (rejections || []).forEach(function (x) {
      var mine = roots.indexOf(x.uuid) >= 0 || (v.visit_uuid && x.visit_uuid === v.visit_uuid) ||
        (x.stop_id === stop.id && (x.type === "visit" || x.type === "parcel"));
      if (!mine) return;
      known = true;
      if (x.state === "open") open = true;
    });
    if (open) return "open";
    if (known) return "closed";
    return roots.length ? "unknown" : "none";
  }

  // a visit with an error whose rejection the server closed takes the
  // server's visit (or goes back to pending when the server has none);
  // returns how many changed. Never one with a change still to send.
  function reconcileVisits(local, route) {
    var changed = 0;
    (route.stops || []).forEach(function (stop) {
      var v = local.visits[stop.id];
      if (!v || !v.error) return;
      if (S.outbox.some(function (e) { return e.route_id === route.id && e.key === "visit:" + stop.id; })) return;
      var status = visitRejection(local, route.rejections, stop, v);
      if (status === "open" || status === "unknown") return;
      if (stop.visit) {
        var adopted = JSON.parse(JSON.stringify(stop.visit));
        adopted.resolved = true;
        local.visits[stop.id] = adopted;
      } else if (status === "closed") {
        delete local.visits[stop.id];
      } else return;
      changed += 1;
    });
    return changed;
  }

  // the poll (app4.js) brings the rejections: a download when one of them
  // unblocks a visit of this phone
  function onRejections(rejections) {
    if (!S.route || !S.local) return;
    var unblocked = stops().some(function (stop) {
      var v = visitOf(stop);
      return v && v.error && visitRejection(S.local, rejections, stop, v) === "closed";
    });
    if (unblocked) download(false);
  }

  function markVisit(ev, error, local) {
    local = local || S.local;
    if (ev.type === "receipt") {
      var rv = local.visits[ev.payload.stop_id];
      if (rv && rv.photo_uuid === ev.payload.photo_uuid) {
        if (!error) rv.receipt_photo = true;
        rv.photo_error = error || null;
      }
      return;
    }
    if (ev.type === "expense") {
      var e = local.expenses[ev.payload.expense_uuid];
      if (e && !ev.payload.deleted) { e.synced = !error; e.error = error || null; }
      return;
    }
    if (ev.type !== "visit" && ev.type !== "parcel") return;
    var v = local.visits[ev.payload.stop_id];
    if (v && v.visit_uuid === ev.payload.visit_uuid) {
      v.synced = !error;
      v.error = error || null;
      if (!error && ev.type === "parcel" && ev.payload.photo) v.has_photo = true;
    }
  }

  // ------------------------------------------------------------ download

  // 19.0.1.113.0: a route the driver opened from «Tus rutas de hoy» (or
  // «Descargar la próxima ruta»): asked by id, and shown even when the route
  // on screen still has changes to send (each route keeps its own)
  var wanted = null, explicitSwitch = false;

  function download(manual) {
    var url = API_ROUTE;
    var asked = wanted || (S.local && S.local.routeId && !S.local.closed ? S.local.routeId : null);
    wanted = null;
    if (asked) url += "?id=" + asked;
    return fetchWithTimeout(url, { credentials: "same-origin", cache: "no-store" }, 30000).then(function (res) {
      if (isLoginPage(res)) { S.auth = false; throw new Error("auth"); }
      return res.json().then(function (data) {
        if (!res.ok) throw new Error(data.error || "Error al descargar la ruta.");
        return data;
      });
    }).then(function (data) {
      S.online = true; S.auth = true; S.user = data.user || "";
      // whose phone is this now: the server says it, never the phone
      if (data.user_id && S.uid !== data.user_id) {
        if (S.uid === null) {
          // the first answer: the state of the older app (or nothing) is
          // this user's, and the route asked for was this phone's
          return adoptUser(data.user_id).then(function () { return applyRoute(data, manual); });
        }
        // another user: the previous one's state and outbox stay on the
        // phone under their user; ask again for this user's own route
        return loadUser(data.user_id).then(function () {
          explicitSwitch = false;
          return download(manual);
        });
      }
      return applyRoute(data, manual);
    }).catch(function (err) {
      if (err.message !== "auth") S.online = navigator.onLine && err.name !== "TypeError";
      if (manual) toast(S.auth ? "No se pudo descargar la ruta: " + err.message : "Tu sesión venció.", true);
    }).then(render);
  }

  function pendingFor(routeId) {
    return S.outbox.filter(function (e) { return e.route_id === routeId; }).length;
  }

  function applyRoute(data, manual) {
    S.otherRoutes = data.routes || [];
    var incoming = data.route;
    var explicit = explicitSwitch;
    explicitSwitch = false;
    // the route asked for is gone (cancelled, another driver's, settled)
    var gone = data.reset && data.gone_id ? noteGone(data.gone_id) : Promise.resolve();
    var sameRoute = incoming && S.route && incoming.id === S.route.id;
    // a route closed on this phone with its close still to send stays on
    // screen until it is sent, unless the driver opens another one
    var keepClosed = S.route && S.local && S.local.closed && pendingFor(S.route.id) && !explicit &&
      !data.reset;
    if (!incoming) {
      if (!S.route || data.reset || (S.local.closed && !pendingFor(S.route.id))) {
        S.route = null;
        S.local = emptyLocal(null);
      }
    } else if (sameRoute) {
      S.route = incoming;
    } else if (!S.route || !keepClosed) {
      // another route: each one keeps its own state on the phone
      if (S.local && S.local.routeId) S.locals[S.local.routeId] = S.local;
      S.route = incoming;
      S.local = fillLocal(S.locals[incoming.id] || emptyLocal(incoming.id));
      S.locals[incoming.id] = S.local;
    }
    if (S.route) {
      if (S.route.state === "closed" || S.route.state === "settled") S.local.closed = true;
      // 19.0.1.115.0: «Reabrir para el repartidor» (or a close that never
      // reached the server): open again once nothing of it is still to send
      else if (S.local.closed && (S.route.state === "on_route" || S.route.state === "draft") &&
               !pendingFor(S.route.id)) S.local.closed = false;
      if (S.route.state === "on_route") S.local.departed = true;
      // the server decides: a route still «Asignada» has not left (an
      // older version of the app marked it on the phone before asking)
      else if (S.route.state === "draft") S.local.departed = false;
      stops().forEach(function (stop) {
        if (!S.local.visits[stop.id] && stop.visit) S.local.visits[stop.id] = stop.visit;
        if (stop.loaded && S.local.loaded[stop.id] === undefined) S.local.loaded[stop.id] = true;
        // after «Controlado» the server's ticks are the ones that count
        if (S.route.controlled) S.local.loaded[stop.id] = !!stop.loaded;
      });
      // 19.0.1.129.22: a rejected visit that administración resolved (or
      // that was resent) takes the server's visit, before the rejections go
      var resolved = reconcileVisits(S.local, S.route);
      if (resolved) {
        toast(resolved === 1 ? "Administración resolvió una visita." :
          "Administración resolvió " + resolved + " visitas.");
      }
      // the server closed some rejections (resent or resolved by administración)
      var closedOnServer = {};
      (S.route.rejections || []).forEach(function (x) { if (x.state !== "open") closedOnServer[x.uuid] = true; });
      S.local.rejected = (S.local.rejected || []).filter(function (x) {
        return x.event && !closedOnServer[x.root];
      });
      S.local.expenses = S.local.expenses || {};
      (S.route.expenses || []).forEach(function (e) {
        if (!S.local.expenses[e.uuid]) S.local.expenses[e.uuid] = e;
      });
    }
    // 19.0.1.129.28: app5.js (the driver's own order, «Mis comprobantes»)
    extHook("routeApplied", data);
    // the list too, so «Tus rutas de hoy» opens without signal
    var keepList = S.uid === null ? Promise.resolve() : kvSet(userKey("routes"), S.otherRoutes);
    return gone.then(pruneLocals).then(saveRoute).then(saveLocal).then(function () { return keepList; }).then(function () {
      if (data.reset && data.gone_id && manual) {
        toast("Esa ruta ya no está en tu reparto. Te mostramos la de ahora.");
      } else if (manual) toast("Ruta actualizada.");
    });
  }

  // a gone route: its pending changes stay (they are still sent, and the
  // server applies them or keeps them in «Cambios rechazados»); without any,
  // the phone forgets it
  function noteGone(routeId) {
    var name = S.route && S.route.id === routeId ? S.route.name : (S.gone && S.gone.id === routeId ? S.gone.name : "");
    // also when its changes just went (sent before the download said it
    // is gone) and some came back rejected: the driver is told
    var rejected = ((S.locals[routeId] || {}).rejected || []).length;
    if (pendingFor(routeId) || rejected) {
      S.gone = { id: routeId, name: name || "" };
      return kvSet(userKey("gone"), S.gone);
    }
    return forgetRoute(routeId);
  }
  function forgetRoute(routeId) {
    delete S.locals[routeId];
    if (S.uid === null) return Promise.resolve();
    return Promise.all([kvDel(userKey("local", routeId)), kvDel(userKey("route", routeId))]);
  }
  // the routes the phone no longer needs: not open on the server, not on
  // screen, nothing left to send and not the gone one being reported
  function pruneLocals() {
    var open = {};
    (S.otherRoutes || []).forEach(function (r) { open[r.id] = true; });
    var jobs = Object.keys(S.locals).map(Number).filter(function (id) {
      return !open[id] && !(S.route && S.route.id === id) && !pendingFor(id) &&
        !(S.gone && S.gone.id === id);
    }).map(forgetRoute);
    return Promise.all(jobs);
  }

  // ------------------------------------------------------------- users

  function byCapture(a, b) { return a.captured_at < b.captured_at ? -1 : 1; }

  // the state of one user, as kept on this phone (19.0.1.113.0)
  function loadUser(uid) {
    var keep = S.local && S.local.routeId && S.uid !== null ? saveLocal() : Promise.resolve();
    return keep.then(function () {
      S.uid = uid; S.locals = {}; S.gone = null; S.route = null; S.local = emptyLocal(null);
      S.otherRoutes = []; S.user = "";
      return kvSet("uid", uid);
    }).then(function () { return Promise.all([kvKeys(), outboxAll()]); }).then(function (res) {
      var prefix = "u:" + uid + ":local:";
      var ids = (res[0] || []).filter(function (k) {
        return typeof k === "string" && k.indexOf(prefix) === 0;
      }).map(function (k) { return +k.slice(prefix.length); });
      // only this user's events: the others stay in IndexedDB, untouched
      S.outbox = (res[1] || []).filter(function (e) { return e.uid === uid; }).sort(byCapture);
      return Promise.all([kvGet(userKey("cur")), kvGet(userKey("gpsNotice")), kvGet(userKey("gone")),
                          kvGet(userKey("routes"))]
        .concat(ids.map(function (id) { return kvGet(userKey("local", id)); }))).then(function (r) {
        S.gpsNotice = r[1] || null;
        S.gone = r[2] || null;
        S.otherRoutes = r[3] || [];
        ids.forEach(function (id, i) { if (r[4 + i]) S.locals[id] = fillLocal(r[4 + i]); });
        return r[0] ? kvGet(userKey("route", r[0])) : null;
      });
    }).then(function (route) {
      S.route = route || null;
      if (S.route) {
        S.local = fillLocal(S.locals[S.route.id] || emptyLocal(S.route.id));
        S.locals[S.route.id] = S.local;
      }
    });
  }

  // the first user the server names takes the state of the older app (the
  // keys "route" and "local" and the events without a user)
  function adoptUser(uid) {
    S.uid = uid;
    var legacy = S.outbox.filter(function (e) { return !e.uid; });
    legacy.forEach(function (e) { e.uid = uid; });
    return kvSet("uid", uid)
      .then(function () { return Promise.all(legacy.map(outboxPut)); })
      .then(function () { return S.route ? saveRoute() : null; })
      .then(saveLocal)
      .then(function () { return S.gpsNotice ? kvSet(userKey("gpsNotice"), S.gpsNotice) : null; })
      .then(function () { return Promise.all([kvDel("route"), kvDel("local"), kvDel("gpsNotice")]); });
  }

  // ------------------------------------------------------------- render

  var app = document.getElementById("app");

  function toast(text, isError) {
    var el = document.getElementById("toast");
    el.textContent = text;
    el.className = isError ? "err" : "";
    el.hidden = false;
    clearTimeout(toast.t);
    toast.t = setTimeout(function () { el.hidden = true; }, 3500);
  }

  // 19.0.1.113.0: a route that is gone while the phone still had changes of it
  function goneBanner() {
    if (!S.gone) return "";
    var n = pendingFor(S.gone.id), name = S.gone.name ? "«" + h(S.gone.name) + "»" : "anterior";
    if (n) {
      return '<div class="banner warn gone">La ruta ' + name + " ya no está en tu reparto (se anuló o pasó a " +
        "otro repartidor). Quedan " + n + (n === 1 ? " cambio" : " cambios") + " de esa ruta en el celular: se " +
        "mandan igual y, si no se pueden aplicar, administración los ve en «Cambios rechazados».</div>";
    }
    return '<div class="banner ok gone"><span>La ruta ' + name + " ya no está en tu reparto (se anuló o pasó a " +
      "otro repartidor): te mostramos la de ahora. Lo que quedaba de esa ruta ya llegó al servidor; si algo no se " +
      'pudo aplicar, lo resuelve administración.</span> <button class="link" data-a="gone-ok">Entendido</button></div>';
  }

  // «Tus rutas de hoy» (19.0.1.113.0): only with more than one open route,
  // so the usual single-route screens stay as they were
  function routesBar() {
    var list = S.otherRoutes || [];
    if (list.length < 2) return "";
    return '<a class="routes-bar" href="#rutas"><span><b>Tus rutas de hoy</b> · ' + list.length +
      " rutas abiertas</span><span>Cambiar ›</span></a>";
  }

  function statusBanner() {
    if (!S.auth) {
      return '<div class="banner err">Tu sesión venció. Lo guardado sigue en el celular. ' +
        '<a href="/web/login?redirect=/reparto">Entrar de nuevo</a></div>';
    }
    var n = S.outbox.length;
    var visits = S.outbox.filter(function (e) { return e.type === "visit" || e.type === "parcel"; }).length;
    var spent = S.outbox.filter(function (e) { return e.type === "expense"; }).length;
    var bits = [];
    if (visits) bits.push(visits + (visits === 1 ? " visita" : " visitas"));
    if (spent) bits.push(spent + (spent === 1 ? " gasto" : " gastos"));
    var what = bits.length ? bits.join(" y ") + (visits + spent === 1 ? " guardado" : " guardados")
                           : n + (n === 1 ? " cambio guardado" : " cambios guardados");
    if (!S.online) {
      return goneBanner() + '<div class="banner warn">' + ICON_NOSIGNAL + "Sin señal" +
        (n ? " · " + what + " en el celular" : "") + "</div>";
    }
    if (n) return goneBanner() + '<div class="banner warn">Enviando · ' + what + " en el celular</div>";
    return goneBanner();
  }
  function paintStatus() {
    var el = document.getElementById("status");
    if (el) el.innerHTML = statusBanner();
  }

  var ICON_CHECK = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5L20 7"></path></svg>';
  var ICON_NOSIGNAL = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 8.8a15 15 0 0 1 20 0"></path><path d="M5 12.5a10 10 0 0 1 14 0"></path><path d="M8.5 16.4a5 5 0 0 1 7 0"></path><path d="M3 3l18 18"></path></svg>';
  var ICON_CAMERA = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h3l2-3h6l2 3h3a1 1 0 0 1 1 1v11a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V8a1 1 0 0 1 1-1z"></path><circle cx="12" cy="13" r="4"></circle></svg>';
  var ICON_PIN = '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#0a6b5c" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 21s-7-6.2-7-12a7 7 0 0 1 14 0c0 5.8-7 12-7 12z"></path><circle cx="12" cy="9" r="2.5"></circle></svg>';

  function render() {
    var y = window.scrollY;
    var hash = location.hash.replace(/^#/, "");
    var parts = hash.split("/");
    // 19.0.1.129.28: app5.js draws its own screens («Reparto v2» only)
    var html = extHook("view", parts[0], parts[1]);
    if (typeof html === "string" && html) { /* app5.js */ }
    else if (parts[0] === "rutas" && (S.otherRoutes || []).length) html = viewRoutes();
    else if (!S.route) html = viewEmpty();
    else if (S.local.closed) html = viewClosed();
    else if (parts[0] === "gasto") html = viewExpenses(parts[1] === "cierre");
    else if (parts[0] === "visita" && stopById(parts[1]) && stopById(parts[1]).parcel) html = viewParcel(stopById(parts[1]));
    else if (parts[0] === "visita" && stopById(parts[1])) html = viewVisit(stopById(parts[1]));
    else if (parts[0] === "cliente" && stopById(parts[1])) html = viewCustomer(stopById(parts[1]));
    else if (parts[0] === "cuenta" && stopById(parts[1])) html = viewAccount(stopById(parts[1]));
    else if (parts[0] === "cierre") html = viewClose();
    else if (parts[0] === "rechazos") html = viewRejections();
    else if (parts[0] === "salida" && !S.local.departed) html = viewDepart();
    else if (parts[0] === "carga" || !S.local.departed) html = viewLoad();
    else html = viewVisits();
    app.innerHTML = html;
    if (render.keepScroll) window.scrollTo(0, y);
    render.keepScroll = false;
  }
  function go(hash) {
    draft = null; custDraft = null; expDraft = null;
    if (confirmState) closeConfirm();
    // 19.0.1.129.32: one history.back() at a time (a double tap on «Guardar»
    // used to step back twice); the next hashchange ends it
    if (backPending) return;
    if (location.hash === hash) render();
    // 19.0.1.129.29 (#100): back to the level it came from, no new entry.
    // 19.0.1.129.32: only to go UP; a forward step («Salir a repartir»)
    // always adds its entry, even if the current one says it came from there
    else if (isUpLevel(hash) && cameFrom(hash)) {
      backPending = true; NAV.back();
      // in case no hashchange comes (the entry left the page)
      setTimeout(function () { backPending = false; }, 1500);
    }
    else location.hash = hash;
    window.scrollTo(0, 0);
  }
  window.addEventListener("hashchange", function () {
    draft = null; custDraft = null; expDraft = null;
    if (confirmState) closeConfirm();
    render(); window.scrollTo(0, 0);
  });

  // nav-up:start (tests/data/navegacion_v129_29.js runs this block)
  // 19.0.1.129.29 (#100): «Atrás» goes UP one level (see the header)
  var NAV = typeof history !== "undefined" && history && history.replaceState ? history : null;
  var upReplacing = false;
  var backPending = false;
  function hashKey(h) { return String(h || "").replace(/^#/, ""); }
  function urlHash(url) { var i = String(url || "").indexOf("#"); return i < 0 ? "" : url.slice(i + 1); }
  // 19.0.1.129.32: the levels go() may reach with history.back(): the lists
  // and a visit. «#salida» is always a step forward.
  function isUpLevel(h) {
    h = hashKey(h);
    return h === "" || h === "visitas" || h === "carga" || /^visita\//.test(h);
  }
  // the visits list is both "" and "visitas" once the driver left
  function sameLevel(a, b) {
    a = hashKey(a); b = hashKey(b);
    if (a === b) return true;
    var list = { "": 1, visitas: 1 };
    return !!(list[a] && list[b] && S.local && S.local.departed && !S.local.closed);
  }
  function cameFrom(hash) {
    var st = NAV && NAV.state;
    return !!(st && typeof st.dnFrom === "string" && sameLevel(st.dnFrom, hash));
  }
  function goUp(hash) {
    if (cameFrom(hash)) { NAV.back(); return; }
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
    window.addEventListener("hashchange", function (ev) {
      backPending = false;
      var st = NAV.state;
      if (!st || !("dnFrom" in st)) {
        var next = {};
        Object.keys(st || {}).forEach(function (k) { next[k] = st[k]; });
        next.dnFrom = upReplacing ? null : urlHash(ev.oldURL);
        NAV.replaceState(next, "");
      }
      upReplacing = false;
    });
    document.addEventListener("click", function (ev) {
      var a = ev.target && ev.target.closest && ev.target.closest("a.back");
      if (!a || ev.defaultPrevented || ev.ctrlKey || ev.metaKey || ev.shiftKey) return;
      var href = a.getAttribute("href") || "";
      if (href.charAt(0) !== "#") return;
      ev.preventDefault();
      goUp(href);
    });
  }
  // nav-up:end

  // --- no route

  function viewEmpty() {
    return '<div class="head"><div class="kicker">Reparto</div><h1>Sin ruta asignada</h1>' +
      '<div class="sub">' + h(S.user) + "</div></div>" +
      '<div id="status">' + statusBanner() + "</div>" + (extHook("homeHtml") || "") +
      '<div class="body"><div class="card"><div>No tenés una ruta asignada. Cuando administración ' +
      "te asigne una, descargala antes de salir, con conexión.</div></div></div>" +
      // 26/11/2026 (owner): only «Rutas anteriores» here; accounts and cash
      // collection are no longer offered from the no-route screen
      '<div class="foot"><button class="btn" data-a="download">Descargar la ruta</button>' +
      '<a class="link cashlink" href="' + PAST_ROUTES + '">Rutas anteriores ›</a></div>';
  }

  // --- truck loading
  //
  // Two people check each order: the driver here and the dispatcher on
  // /despacho. «Salir a repartir» needs both ticks on every order and a
  // connection (there is one at the depot): the server has the last word.

  function loadInfo(s) {
    var on = !!S.local.loaded[s.id], d = !!s.dispatch_checked;
    if (on && d) return { cls: "ok", text: "Controlado por vos y por " + (s.dispatch_by || "el depósito") };
    if (on) return { cls: "warn", text: "Falta el control del depósito" };
    if (d) return { cls: "warn", text: (s.dispatch_by || "El depósito") + " ya lo controló · falta que lo tildes vos" };
    return { cls: "none", text: "Sin tildar" };
  }
  function doubleChecked(s) { return !!S.local.loaded[s.id] && !!s.dispatch_checked; }

  function leftHtml() {
    var left = (S.route && S.route.left) || [];
    if (!left.length) return "";
    return '<div class="card" style="gap:6px"><div class="q">Quedan en el depósito</div>' + left.map(function (l) {
      return '<div class="small"><b>' + h(l.name) + "</b> · " + h(l.order) + " · " + h(l.reason) + "</div>";
    }).join("") + "</div>";
  }

  function viewLoad() {
    var list = stops();
    var total = 0, loaded = 0, both = 0;
    var rows = list.map(function (s) {
      var on = !!S.local.loaded[s.id], info = loadInfo(s);
      total += s.parcels || 0;
      if (on) loaded += s.parcels || 0;
      if (doubleChecked(s)) both += 1;
      return '<button class="load' + (on ? " on" : "") + (info.cls === "warn" ? " mismatch" : "") +
        '" data-a="load" data-id="' + s.id + '"' + (S.route.controlled ? " disabled" : "") + ">" +
        '<span class="box">' + (on ? ICON_CHECK : "") + "</span>" +
        '<span class="who"><b>' + h(s.name) + "</b><span>" +
        h([s.parcel ? "Encomienda · " + s.parcel.town : s.address, s.order].filter(Boolean).join(" · ")) +
        '</span><span class="dsp ' + info.cls + '">' + h(info.text) + "</span></span>" +
        '<span class="n">' + (s.parcels || 0) + (s.parcels === 1 ? " bulto" : " bultos") + "</span></button>";
    }).join("");
    var ready = list.length > 0 && both === list.length;
    var hint = !S.online ? "Para salir necesitás conexión." :
      ready ? "Todo controlado por vos y por el depósito." :
      "Se habilita cuando cada pedido está tildado por vos y por el depósito.";
    return '<div class="head"><div class="kicker">Ruta de ' + h(dateLabel(S.route.date)) + " · Viaje " + h(S.route.trip) +
      "</div><h1>" + h(S.route.zones || "Ruta") + '</h1><div class="sub">' + list.length +
      (list.length === 1 ? " pedido · " : " pedidos · ") + total + " bultos</div></div>" +
      '<div id="status">' + statusBanner() + "</div>" + routesBar() +
      (S.departError ? '<div class="banner err depart-err"><span>' + h(S.departError) + "</span></div>" : "") +
      (S.route.controlled ? '<div class="banner ok">' + ICON_CHECK + "La carga ya fue controlada con Administración.</div>" : "") +
      '<div class="intro"><h2>Control de carga</h2><p>Tocá cada pedido cuando sube a la camioneta. ' +
      "El depósito lo controla también: tienen que estar los dos tildes.</p></div>" +
      '<div class="body">' + (rows || '<div class="card">La ruta no tiene pedidos.</div>') + leftHtml() +
      '<button class="link" data-a="download">Actualizar la ruta</button></div>' +
      '<div class="foot"><div class="split"><span class="muted">Pedidos con doble control</span><b>' + both + " de " +
      list.length + "</b></div>" +
      '<div class="split"><span class="muted">Bultos cargados</span><b>' + loaded + " de " + total + "</b></div>" +
      '<button class="btn" data-a="depart"' + (ready && S.online && !S.departing ? "" : " disabled") + ">" +
      (S.departing ? "Saliendo…" : "Salir a repartir") + "</button>" +
      '<div class="hint">' + h(hint) + "</div></div>";
  }

  // «Salir a repartir», step 2: the visit order. What went up first is at
  // the back of the truck, so the app offers the loading order reversed. The
  // driver chooses; nothing is reordered without asking.
  // the server's loading order (the one it applies); a tick still in the
  // outbox goes after, in the order it was made on the phone
  function loadRank(s) {
    var pending = S.outbox.some(function (e) { return e.key === "load:" + s.id; });
    if (!pending && s.load_sequence) return s.load_sequence;
    var i = (S.local.loadOrder || []).indexOf(s.id);
    return 100000 + (i >= 0 ? i : 0);
  }
  function viewDepart() {
    var proposed = stops().slice().sort(function (a, b) { return loadRank(b) - loadRank(a); });
    var rows = proposed.map(function (s, i) {
      return '<div class="visit"><div class="split"><span class="t">' + (i + 1) + " · " + h(s.name) + "</span>" +
        '<span class="pill">' + (s.parcels || 0) + (s.parcels === 1 ? " bulto" : " bultos") + "</span></div>" +
        '<div class="d">' + h(s.parcel ? "Encomienda · " + s.parcel.town : s.address) + "</div></div>";
    }).join("");
    var busy = S.departing ? " disabled" : "";
    return '<div class="head"><a class="back" href="#carga">‹ Control de carga</a><h1>Orden de las visitas</h1>' +
      '<div class="sub">Lo que cargaste primero quedó al fondo de la camioneta</div></div>' +
      '<div id="status">' + statusBanner() + "</div>" +
      '<div class="intro"><h2>¿Visitás al revés de cómo cargaste?</h2><p>Así lo último que subió se entrega ' +
      "primero. Si preferís el orden que armó administración, salí con el orden de la ruta.</p></div>" +
      '<div class="body">' + rows + "</div>" +
      '<div class="foot"><button class="btn teal" data-a="depart-go" data-v="1"' + busy + ">" +
      (S.departing ? "Saliendo…" : "Sí, visitar en este orden y salir") + "</button>" +
      '<button class="btn outline" data-a="depart-go" data-v="0"' + busy + ">No, salir con el orden de la ruta</button>" +
      '<div class="hint">Para salir necesitás conexión.</div></div>';
  }

  // online only: first the pending ticks, then the departure itself, and
  // the answer decides (the server rejects it without the double check)
  function departNow(reverse, gps) {
    if (S.departing) return;
    S.departing = true; S.departError = null; rerender();
    syncNow().then(function () {
      if (S.outbox.some(function (e) { return e.type === "load"; })) {
        throw new Error("No se pudieron enviar tus tildes de carga. Conectate a internet y probá de nuevo.");
      }
      var payload = { reverse_load_order: !!reverse };
      if (gps !== undefined) payload.gps = gps;
      var ev = { uuid: uuid(), type: "depart", route_id: S.route.id, captured_at: new Date().toISOString(),
                 payload: payload };
      return fetchWithTimeout(API_SYNC, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ events: [ev] }),
      }, 20000);
    }).then(function (res) {
      if (isLoginPage(res)) { S.auth = false; throw new Error("Tu sesión venció: entrá de nuevo para salir."); }
      return res.json();
    }).then(function (data) {
      var r = (data.results || [])[0] || {};
      if (r.status !== "ok" && r.status !== "duplicate") {
        throw new Error(r.error || data.error || "El servidor no aceptó la salida.");
      }
      S.departing = false; S.local.departed = true;
      return saveLocal().then(function () {
        toast("Salida registrada. ¡Buen viaje!");
        go("#visitas");
        return download(false);
      });
    }).catch(function (err) {
      S.departing = false;
      var offline = err && (err.name === "TypeError" || err.name === "AbortError");
      if (offline) S.online = false;
      S.departError = offline ? "Sin conexión. Conectate a internet y probá de nuevo: para salir necesitás conexión." :
        (err && err.message) || "No se pudo salir.";
      go("#carga");
    });
  }

  function onLoadScreen() {
    var part = location.hash.replace(/^#/, "").split("/")[0];
    return S.route && !S.local.departed && !S.local.closed && (part === "" || part === "carga" || part === "salida");
  }

  // the dispatcher's ticks, while the truck is being loaded
  function pollLoad() {
    if (!onLoadScreen() || document.visibilityState !== "visible" || S.departing) return;
    var routeId = S.route.id;
    return fetchWithTimeout(API_LOAD + "?id=" + routeId, { credentials: "same-origin", cache: "no-store" }, 10000)
      .then(function (res) {
        if (isLoginPage(res)) { S.auth = false; throw new Error("auth"); }
        return res.json();
      }).then(function (data) {
        if (!data.ok || !data.route || !S.route || S.route.id !== routeId) return;
        S.online = true; S.auth = true;
        var r = data.route;
        // 19.0.1.129.16: the payments of each visit (app4.js)
        extHook("onLoadPoll", r);
        var theirs = r.stops.map(function (x) { return x.id; }).sort().join(",");
        var mine = stops().map(function (x) { return x.id; }).sort().join(",");
        if (theirs !== mine || r.state !== S.route.state || !!r.controlled !== !!S.route.controlled) {
          return download(false);
        }
        var changed = false;
        r.stops.forEach(function (x) {
          var s = stopById(x.id);
          if (s.dispatch_checked !== x.dispatch_checked || s.dispatch_by !== x.dispatch_by ||
              s.load_sequence !== x.load_sequence) {
            s.dispatch_checked = x.dispatch_checked; s.dispatch_by = x.dispatch_by;
            s.load_sequence = x.load_sequence; changed = true;
          }
        });
        if (JSON.stringify(r.left || []) !== JSON.stringify(S.route.left || [])) {
          S.route.left = r.left || []; changed = true;
        }
        if (changed) {
          saveRoute();
          if (onLoadScreen() && location.hash.indexOf("#salida") !== 0) rerender();
        } else paintStatus();
      }).catch(function (err) {
        if (!err || err.message !== "auth") S.online = false;
        paintStatus();
      });
  }

  // --- visits list

  var EVENT_LABELS = { visit: "Visita", parcel: "Encomienda", expense: "Gasto", customer: "Ubicación y horarios",
                       whatsapp: "WhatsApp del cliente", load: "Carga", depart: "Salida", close: "Cierre de la ruta",
                       receipt: "Foto del comprobante" };
  function viewRejections() {
    var list = openRejections();
    var cards = list.map(function (x) {
      var ev = x.event, p = ev.payload || {}, stop = p.stop_id ? stopById(p.stop_id) : null;
      var what = [EVENT_LABELS[ev.type] || ev.type, stop ? stop.name + " · " + stop.order : ""];
      if (p.cash > 0) what.push("efectivo " + money(p.cash));
      if (p.amount > 0) what.push(money(p.amount));
      if (p.transfer_amount > 0) what.push("transferencia " + money(p.transfer_amount));
      var fix = "";
      if ((ev.type === "visit" || ev.type === "parcel") && stop) {
        fix = '<a class="opt primary" href="#visita/' + stop.id + '">Corregir y guardar de nuevo</a>';
      } else if (ev.type === "receipt" && stop) {
        fix = '<a class="opt primary" href="#visita/' + stop.id + '">Sacar la foto de nuevo</a>';
      } else if (ev.type === "expense") {
        fix = '<a class="opt primary" href="#gasto">Corregir el gasto</a>';
      }
      return '<div class="card warn" style="gap:8px"><div class="q">' + h(what.filter(Boolean).join(" · ")) + "</div>" +
        '<div class="small"><b>Por qué se rechazó:</b> ' + h(x.error) + "</div>" +
        '<div class="small muted">Guardado el ' + h(shortDate(ev.captured_at)) + "</div>" + fix +
        '<button class="opt second" data-a="rej-resend" data-id="' + h(x.root) + '">Reenviar igual</button></div>';
    }).join("");
    return '<div class="head"><a class="back" href="#visitas">‹ Visitas</a><h1>Cambios rechazados</h1>' +
      '<div class="sub">' + list.length + (list.length === 1 ? " cambio sin resolver" : " cambios sin resolver") + "</div></div>" +
      '<div id="status">' + statusBanner() + "</div>" +
      '<div class="body">' + (cards || '<div class="card">No hay cambios rechazados.</div>') +
      '<div class="note">No se pueden borrar: quedan acá hasta que los reenvíes corregidos o administración los ' +
      "marque resueltos. Administración también los ve, y la rendición no se cierra con uno pendiente.</div></div>";
  }

  function viewVisits() {
    var list = stops();
    var done = list.filter(function (s) { return stopState(s) !== "pending"; });
    var next = list.filter(function (s) { return stopState(s) === "pending"; })[0];
    var pct = list.length ? Math.round(done.length * 100 / list.length) : 0;
    var rejList = openRejections(), shown = rejList.filter(function (x) { return !x.hidden; });
    var rejected = shown.length ?
      '<div class="banner err"><span>El servidor rechazó ' + shown.length +
      (shown.length === 1 ? " cambio" : " cambios") + ": " + h(shown[shown.length - 1].error) +
      ' </span><a class="link" href="#rechazos">Ver</a> <button class="link" data-a="dismiss">Entendido</button></div>' :
      (rejList.length ? '<a class="banner err" href="#rechazos">Cambios rechazados sin resolver: ' + rejList.length +
        " · tocá para verlos</a>" : "");
    var cards = list.map(function (s, i) {
      var st = stopState(s), v = visitOf(s), c = customerOf(s);
      var n = (i + 1) + " · " + h(s.name);
      var detail;
      if (s.parcel && (st === "pending" || st === "dispatched")) {
        detail = parcelTag(s);
        if (st === "dispatched") {
          detail += '<div class="d">Guía N° ' + h(v.guide_number || "(en la foto)") +
            (v.cost > 0 ? " · Envío " + money(v.cost) : "") + "</div>";
        }
      } else if (st === "pending") {
        var bits = [s.address, (s.parcels || 0) + (s.parcels === 1 ? " bulto" : " bultos")];
        var when = daysHoursText(c);
        if (s === next) {
          if (when) bits.push(when);
        } else {
          bits.push("Saldo " + money(balance(s, [])));
        }
        detail = '<div class="d">' + h(bits.filter(Boolean).join(" · ")) + "</div>";
        if (s === next) {
          detail += '<div class="money-row"><span>Saldo total</span><b>' + money(balance(s, [])) + "</b></div>";
        }
      } else if (st === "paid") {
        var pays = [];
        if (v.cash > 0) pays.push("Efectivo " + money(v.cash));
        if (v.transfer === "yes" || v.transfer === "done") {
          pays.push("Transferencia " + money(v.transfer_amount || 0) + " · comprobante " + receiptLabel(s, v));
        }
        detail = '<div class="d">' + h(pays.join(" · ")) + "</div>";
      } else if (st === "not_paid") {
        detail = '<div class="d">Sin cobrar · ' + money(dueToday(s, v.returns)) + " a tu cargo</div>";
      } else if (st === "already_paid") {
        detail = v.to_confirm ? '<div class="d">Entregado · ya estaba pagado, a confirmar por administración</div>' :
          '<div class="d">Entregado · ya estaba pagado, sin cobro</div>';
      } else {
        detail = '<div class="d">' + (v.reason ? "Motivo: " + h(v.reason) : "Falta el motivo") + "</div>";
      }
      if (st === "pending") detail += noticeHtml(s);
      customerItems(s.commercial_partner_id).filter(function (r) { return r.stop_id !== s.id; })
        .forEach(function (r) { detail += itemHtml(r); });
      if (st === "pending" && redOf(s)) {
        detail += '<div class="redline">En rojo · reclamá lo que debe antes de descargar</div>';
      }
      if (v && v.error) detail += '<div class="e">No se envió: ' + h(v.error) + " Abrí la visita y guardala de nuevo.</div>";
      if (v && !v.error && v.warning) detail += '<div class="e">' + h(v.warning) + "</div>";
      if (v && v.resolved) detail += '<div class="okline">' + ICON_CHECK + "Administración resolvió esta visita</div>";
      var pill = s === next ? "SUGERIDA" : (s.parcel && st === "pending" ? "ENCOMIENDA" : PILL[st]);
      if (st === "already_paid" && v && v.to_confirm) pill = "A CONFIRMAR";
      if (st === "pending" && redOf(s)) pill = "EN ROJO";
      var cls = "visit" + (st !== "pending" ? " done" : "") + (s === next ? " next" : "");
      return '<a class="' + cls + '" href="#visita/' + s.id + '"><div class="split"><span class="t">' + n +
        '</span><span class="pill ' + (st === "pending" && redOf(s) ? "red" : st) + '">' + pill + "</span></div>" +
        detail + "</a>";
    }).join("");
    return '<div class="head"><div class="row"><h1>Visitas</h1><div class="sub">' + done.length + " de " +
      list.length + " hechas</div></div>" +
      '<div class="bar"><span style="width:' + pct + '%"></span></div>' +
      '<div class="sub small">Orden sugerido. Tocá el cliente que vas a visitar.' + (extHook("visitsTools") || "") +
      "</div></div>" +
      '<div id="status">' + statusBanner() + "</div>" + routesBar() + (extHook("homeHtml") || "") + rejected +
      '<div class="body">' + cards + visitsMore(done.length === list.length) + "</div>";
  }

  // 26/11/2026 (drivers, via the owner): the visits list is the screen they
  // use most, so no fixed footer covering half of it. «Cargar gasto»,
  // «Cerrar la ruta» and «Rutas anteriores» live at the END of the list,
  // folded under «Más opciones» so they are not tapped by mistake; «Cerrar la
  // ruta» shows as a button there only once every visit is done. Cash
  // collection and customer accounts are no longer offered from here.
  function visitsMore(allDone) {
    return (allDone ? '<div class="more-close"><a class="btn outline" href="#cierre">Cerrar la ruta</a></div>' : "") +
      '<details class="more"><summary>Más opciones</summary><div class="more-body">' +
      '<a class="btn outline" href="#gasto">Cargar gasto</a>' +
      (allDone ? "" : '<a class="btn outline" href="#cierre">Cerrar la ruta</a>') +
      '<a class="link cashlink" href="' + PAST_ROUTES + '">Rutas anteriores ›</a></div></details>';
  }

  // --- «Ver cuenta» (19.0.1.92.0): the customer's balance and newest
  // movements, read online; the server only answers for the driver's own stops

  var acct = { stopId: null, data: null, error: "", loading: false };

  function accountLink(stop) {
    // 19.0.1.120.0: a wholesale customer's account stays with administración
    if (stop.account === false) return "";
    return '<a class="place" href="#cuenta/' + stop.id + '"><span class="txt"><b>Ver cuenta</b>' +
      "<span>Saldo y últimos movimientos del cliente (necesita señal)</span></span>" +
      '<span class="chev">›</span></a>';
  }

  function loadAccount(stop) {
    acct = { stopId: stop.id, data: null, error: "", loading: true };
    fetchWithTimeout(API_ACCOUNT + "?stop=" + stop.id, { credentials: "same-origin", cache: "no-store" }, 20000)
      .then(function (res) {
        if (isLoginPage(res)) throw new Error("Tu sesión venció: volvé a entrar con tu usuario.");
        return res.json();
      })
      .then(function (data) {
        if (!data.ok) throw new Error(data.error || "No se pudo cargar la cuenta.");
        acct.data = data.account;
      })
      .catch(function (err) {
        acct.error = err && err.message && err.name !== "AbortError" && err.name !== "TypeError" ? err.message :
          "Sin señal: la cuenta se consulta en línea. Probá de nuevo cuando tengas señal.";
      })
      .then(function () {
        acct.loading = false;
        if (location.hash === "#cuenta/" + stop.id) rerender();
      });
  }

  function viewAccount(stop) {
    if (acct.stopId !== stop.id) loadAccount(stop);
    var out = '<div class="head"><a class="back" href="#visita/' + stop.id + '">‹ Visita</a><h1>Cuenta</h1>' +
      '<div class="sub">' + h(stop.name) + "</div></div>" + '<div class="body">';
    var a = acct.data;
    if (acct.loading) {
      out += '<div class="card muted">Cargando la cuenta…</div>';
    } else if (acct.error) {
      out += '<div class="banner err">' + h(acct.error) + "</div>";
    } else if (a) {
      out += '<div class="card"><div class="money-row"><span>' + (a.balance < -0.004 ? "Saldo a favor" : "Saldo") +
        "</span><b>" + money(Math.abs(a.balance)) + "</b></div>" +
        (a.provisional ? '<div class="small muted">Incluye devoluciones informadas a controlar por ' +
          money(a.provisional) + "</div>" : "") + "</div>";
      out += '<div class="label">Últimos movimientos' + (a.row_count > a.rows.length ? " (" + a.rows.length +
        " de " + a.row_count + ")" : "") + "</div>";
      out += a.rows.length ? a.rows.map(function (r) {
        return '<div class="card acctrow"><div class="split"><span><b>' + h(r.type) + "</b> " + h(r.ref) +
          '</span><span class="' + (r.credit ? "credit" : "") + '">' +
          (r.debit ? money(r.debit) : "- " + money(r.credit)) + "</span></div>" +
          '<div class="small muted">' + h([r.date.split("-").reverse().join("/"), r.concept, "Saldo " + money(r.balance),
            r.provisional ? "a controlar" : ""].filter(Boolean).join(" · ")) + "</div></div>";
      }).join("") : '<div class="card muted">Sin movimientos.</div>';
      out += '<a class="link cashlink" href="' + ACCOUNTS_APP + '">Ver la cuenta completa en Cuentas ›</a>';
    }
    return out + "</div>" + '<div class="foot"><div class="grid2"><button class="btn outline" data-a="acct-reload"' +
      (acct.loading ? " disabled" : "") + '>Actualizar</button><a class="btn teal" href="' + CASH_PAGE +
      '">Cobro en efectivo</a></div></div>';
  }

  // "Encomienda · Terminal de ómnibus", the carrier and who pays, as the
  // driver tells the carrier how to fill in the guide
  function parcelTag(s) {
    var p = s.parcel;
    return '<div class="parcel-tag"><b>Encomienda · Terminal de ómnibus</b>' +
      "<span>" + h(["Destino: " + p.town, p.carrier].filter(Boolean).join(" · ")) + "</span>" +
      '<span class="payer ' + (p.payer === "distri" ? "distri" : "customer") + '">' + h(p.payer_label) + "</span>" +
      (p.recipient ? '<span class="who-picks">Retira: <b>' + h(p.recipient) + "</b>" +
        (p.dni ? " · DNI <b>" + h(p.dni) + "</b>" : "") + "</span>" : "") + "</div>";
  }

  // --- in-app confirm (a sheet over the screen, like /despacho)

  var confirmState = null;
  function askConfirm(opts) { confirmState = opts; paintConfirm(); }
  function closeConfirm() { confirmState = null; paintConfirm(); }
  function paintConfirm() {
    var el = document.getElementById("confirm");
    if (!el) {
      el = document.createElement("div"); el.id = "confirm";
      document.body.appendChild(el);
    }
    var c = confirmState;
    el.innerHTML = !c ? "" : '<div class="sheet-bg"><div class="sheet" role="alertdialog" aria-modal="true">' +
      "<h2>" + h(c.title) + '</h2><p class="sheet-text">' + h(c.text) + "</p>" +
      '<div class="grid2"><button class="btn outline" data-a="confirm-no">' + h(c.cancel || "Volver") + "</button>" +
      '<button class="btn' + (c.danger ? " red" : "") + '" data-a="confirm-yes">' + h(c.ok) + "</button></div></div></div>";
  }

  // what «Guardar: no se entregó» throws away, in words
  // what became of the receipt of a saved visit, for the visits list
  function receiptLabel(stop, v) {
    if (v.receipt_answer !== "photo") return RECEIPT_LABELS[v.receipt_answer] || "pendiente";
    if (v.photo_error) return "la foto no se envió";
    if (S.outbox.some(function (e) { return e.key === "receipt:" + stop.id; })) return "foto por enviar";
    if (v.receipt_photo) return "foto enviada";
    // 19.0.1.99.0 (audit F15): the photo still on this phone is not «sin foto»
    return v.thumb || v.photo_uuid ? "foto guardada en el celular" : "sin foto";
  }

  function discardedText(d) {
    var parts = [];
    if (d.code_ok || (d.code || "").length) parts.push("código");
    else if (d.without_code) parts.push("«Entregar igual» sin código");
    var n = d.returns_answer === "yes" ? (d.returns || []).length : 0;
    if (n) parts.push(n + (n === 1 ? " devolución" : " devoluciones"));
    if (d.cash > 0) parts.push("efectivo " + money(d.cash));
    if (d.transfer === "yes") parts.push("transferencia de " + money(d.transfer_amount || 0));
    if (d.transfer === "yes" && d.receipt_answer === "photo" && d.photo) parts.push("foto del comprobante");
    return parts.join(", ");
  }
  function parcelDiscardedText(d) {
    var parts = [];
    if ((d.guide_number || "").trim()) parts.push("número de guía");
    if (d.cost > 0) parts.push("costo del envío " + money(d.cost));
    return parts.join(", ");
  }

  // --- location at the key moments of the route (19.0.1.35.0)

  var GPS_TIMEOUT_MS = 20000, GPS_MAX_AGE_MS = 60000;
  var GPS_ERRORS = { 1: "denied", 2: "unavailable", 3: "timeout" };
  var gpsState = null; // the sheet: notice, searching or blocked
  function gpsConf() { return (S.route && S.route.gps) || null; }
  // only during the route: «Salir» starts it, «Cerrar la ruta» is its end
  function gpsActive(kind) {
    if (!S.local) return false;
    return kind === "start" ? !S.local.departed : S.local.departed && !S.local.closed;
  }
  function noticeAccepted() {
    var c = gpsConf();
    return !c || c.notice_accepted || S.gpsNotice === c.notice_version;
  }
  // one position, fresh (at most a minute old), never a watch
  function readPosition() {
    return new Promise(function (resolve, reject) {
      if (!navigator.geolocation) return reject("unsupported");
      navigator.geolocation.getCurrentPosition(function (pos) {
        resolve({ lat: pos.coords.latitude, lng: pos.coords.longitude,
                  acc: Math.round(pos.coords.accuracy || 0),
                  at: new Date(pos.timestamp || Date.now()).toISOString() });
      }, function (e) { reject(GPS_ERRORS[e && e.code] || "unavailable"); },
      { enableHighAccuracy: true, timeout: GPS_TIMEOUT_MS, maximumAge: GPS_MAX_AGE_MS });
    });
  }
  // Runs fn(gps) with a fresh position. gps is undefined when nothing is
  // captured (outside the route, or an older server), {error} when the GPS
  // is not required and failed. Required and failed: the action waits.
  function withGps(kind, label, fn) {
    var conf = gpsConf();
    if (!conf || !gpsActive(kind)) return fn(undefined);
    if (!noticeAccepted()) {
      gpsState = { notice: true, kind: kind, label: label, fn: fn };
      return paintGps();
    }
    gpsState = { busy: true, label: label }; paintGps();
    readPosition().then(function (gps) {
      gpsState = null; paintGps(); fn(gps);
    }, function (code) {
      if (!conf.required) { gpsState = null; paintGps(); return fn({ error: code }); }
      gpsState = { error: code, kind: kind, label: label, fn: fn }; paintGps();
    });
  }
  function gpsHelp(code) {
    if (code === "denied") {
      return "El celular no le da la ubicación a la app. 1) Activá la Ubicación del celular: bajá la barra " +
        "de arriba y tocá «Ubicación». 2) En Chrome tocá el candado junto a la dirección › Permisos › " +
        "Ubicación › Permitir. Después tocá «Reintentar».";
    }
    if (code === "unsupported") return "Este celular no da la ubicación a la app.";
    return "No se pudo tomar la ubicación. Activá la Ubicación del celular (bajá la barra de arriba y tocá " +
      "«Ubicación»). Bajo techo tarda más: acercate a una puerta o ventana y tocá «Reintentar».";
  }
  function noticeText() {
    var c = gpsConf() || {};
    return "Mientras la ruta está en curso, la app registra tu ubicación solo en cuatro momentos: al salir a " +
      "repartir, al tocar «Llegué» en un cliente, al cerrar cada visita y al cerrar la ruta. No te sigue " +
      "durante el día ni registra nada fuera del trabajo. La empresa usa estos datos para los tiempos de " +
      "reparto y para confirmar las entregas. Las coordenadas se borran a los " + (c.retention_months || 12) +
      " meses. Podés pedirle a administración ver tus datos. Más detalle en la política de privacidad.";
  }
  function paintGps() {
    var el = document.getElementById("gps-sheet");
    if (!el) {
      el = document.createElement("div"); el.id = "gps-sheet";
      document.body.appendChild(el);
    }
    var g = gpsState, inner = "";
    if (g && g.notice) {
      inner = "<h2>Aviso de ubicación</h2><p class=\"sheet-text\">" + h(noticeText()) + "</p>" +
        '<a class="link" href="/privacidad" target="_blank" rel="noopener">Política de privacidad</a>' +
        '<div class="grid2"><button class="btn outline" data-a="gps-cancel">Ahora no</button>' +
        '<button class="btn" data-a="gps-accept">Acepto</button></div>';
    } else if (g && g.busy) {
      inner = "<h2>Tomando tu ubicación…</h2><p class=\"sheet-text\">Para " + h(g.label) +
        " se registra dónde estás. Tarda unos segundos.</p>";
    } else if (g) {
      inner = "<h2>Falta la ubicación</h2><p class=\"sheet-text\">Para " + h(g.label) +
        " la app necesita tu ubicación. " + h(gpsHelp(g.error)) + "</p>" +
        '<p class="sheet-text small">Si no se puede, avisá a administración: puede autorizar la ruta sin GPS.</p>' +
        '<div class="grid2"><button class="btn outline" data-a="gps-cancel">Cancelar</button>' +
        '<button class="btn" data-a="gps-retry">Reintentar</button></div>';
    }
    el.innerHTML = inner ? '<div class="sheet-bg"><div class="sheet" role="alertdialog" aria-modal="true">' +
      inner + "</div></div>" : "";
  }

  // 19.0.1.127.0: who packed the order, when, in how many packages and what
  // was missing: «Armado por Juan 10:42 · 3 bultos · Faltó: 2 Coca 2,25»
  function packedHtml(stop) {
    var missing = stop.missing || [];
    if (!stop.packed_by && !missing.length) return "";
    var parts = [];
    if (stop.packed_by) parts.push("Armado por " + stop.packed_by + (stop.packed_at ? " " + stop.packed_at : ""));
    if (stop.parcels) parts.push(stop.parcels + (stop.parcels === 1 ? " bulto" : " bultos"));
    if (missing.length) {
      parts.push("Faltó: " + missing.map(function (m) { return qty(m.qty) + " " + m.product; }).join(", "));
    }
    return '<div class="packed">' + h(parts.join(" · ")) + "</div>";
  }

  // «Llegué»: the first thing at a customer, before the visit form
  function needsArrival(stop) {
    return !!gpsConf() && !stop.parcel && !!S.local.departed && !S.local.closed && !visitOf(stop) &&
      !(S.local.arrivals || {})[stop.id];
  }
  function viewArrive(stop) {
    var c = customerOf(stop);
    return '<div class="head"><a class="back" href="#visitas">‹ Visitas</a><h1>' + h(stop.name) + "</h1>" +
      '<div class="sub">' + h([stop.address, stop.order, (stop.parcels || 0) + (stop.parcels === 1 ? " bulto" : " bultos")]
        .filter(Boolean).join(" · ")) + "</div></div>" +
      '<div id="status">' + statusBanner() + "</div>" +
      packedHtml(stop) +
      '<a class="place" href="#cliente/' + stop.id + '">' + ICON_PIN +
      '<span class="txt"><b>Ubicación y horarios</b><span>' +
      h([daysHoursText(c), (c.note || "").split("\n")[0]].filter(Boolean).join(" · ") || "Sin datos cargados") +
      '</span></span><span class="chev">›</span></a>' + accountLink(stop) + noticeHtml(stop, "place-notice") +
      '<div class="intro"><h2>¿Ya estás en el cliente?</h2><p>Tocá «Llegué» al llegar: se registran la hora ' +
      "y tu ubicación. Después cargás la visita.</p></div>" +
      '<div class="foot"><button class="btn teal" data-a="arrive">Llegué</button></div>';
  }

  // --- one visit

  function returnsAnswered(d) {
    return d.returns_answer === "none" || (d.returns_answer === "yes" && d.returns.length > 0);
  }

  function newDraft(stop) {
    var v = visitOf(stop);
    if (v) return JSON.parse(JSON.stringify(v));
    return {
      visit_uuid: uuid(), stop_id: stop.id, outcome: "delivered", reason: "",
      returns_answer: null, returns: [], code: "", code_ok: false, without_code: false,
      cash: 0, transfer: "no", transfer_amount: 0, receipt_answer: null, red_ok: false,
      no_payment: false, already_paid: false, to_confirm: false, comment: "",
      // the receipt photo: «photo» only until it is queued, the thumbnail stays
      photo: null, thumb: null, photo_uuid: null, receipt_photo: false,
    };
  }

  function viewVisit(stop) {
    if (needsArrival(stop)) return viewArrive(stop);
    if (!draft || draft.stop_id !== stop.id) {
      draft = newDraft(stop);
      draft.adding = !draft.returns.length;
    }
    var d = draft, c = customerOf(stop), wa = whatsappOf(stop);
    var notDelivered = d.outcome === "not_delivered";
    var lines = stop.lines, shown = d.showAll ? lines : lines.slice(0, 4);
    var retAmount = returnsAmount(stop, d.returns);
    var answered = returnsAnswered(d);
    var noCode = !stop.code_hash;

    var out = '<div class="head"><a class="back" href="#visitas">‹ Visitas</a><h1>' + h(stop.name) + "</h1>" +
      '<div class="sub">' + h([stop.address, stop.order, (stop.parcels || 0) + (stop.parcels === 1 ? " bulto" : " bultos")]
        .filter(Boolean).join(" · ")) + "</div></div>" +
      '<div id="status">' + statusBanner() + "</div>" +
      packedHtml(stop) +
      '<a class="place" href="#cliente/' + stop.id + '">' + ICON_PIN +
      '<span class="txt"><b>Ubicación y horarios</b><span>' +
      h([daysHoursText(c), (c.note || "").split("\n")[0]].filter(Boolean).join(" · ") || "Sin datos cargados") +
      '</span></span><span class="chev">›</span></a>' + accountLink(stop) + noticeHtml(stop, "place-notice") +
      '<div class="body">';

    if (notDelivered) {
      out += '<div class="label">No se pudo entregar</div><div class="card">' +
        '<label class="stack">¿Por qué no se entregó? (obligatorio al cerrar la ruta)' +
        '<input class="inp" data-f="reason" value="' + h(d.reason) + '" placeholder="Ej.: estaba cerrado"></label>' +
        '<div class="small muted">Los ' + (stop.parcels || 0) + " bultos vuelven al depósito.</div>" +
        '<button class="link" data-a="undo-nd">Volver: sí se entregó</button></div>' +
        "</div>" + '<div class="foot"><button class="btn" data-a="save-nd">Guardar: no se entregó</button></div>';
      return out;
    }

    // «Cliente en rojo»: first what he owes, then the delivery
    var red = redOf(stop);
    if (red) {
      out += '<div class="label">Antes de descargar</div><div class="card redcard" style="gap:8px">' +
        '<div class="q">Cliente en rojo · reclamá lo que falta</div>' +
        (red.debt > 0 ? '<div class="money-row"><span>Debe de días anteriores</span><b>' + money(red.debt) + "</b></div>" : "") +
        (red.receipts || []).map(itemHtml).join("") +
        '<div class="small">Cobrá lo que debe y reenviá a la empresa los comprobantes que faltan. Después descargá.</div>' +
        '<button class="opt' + (d.red_ok ? " on" : "") + '" data-a="red-ok">Ya cobré lo que debía y reenvié los comprobantes</button>' +
        '<div class="small red-text">Entregar a un cliente en rojo sin cobrar lo que debe es falta grave. Si no paga, guardá ' +
        "«No se pudo entregar».</div></div>";
    }

    // delivery
    out += '<div class="label">Entrega</div><div class="card">' +
      '<div class="split"><span style="font-size:22px;font-weight:800">' + (stop.parcels || 0) +
      (stop.parcels === 1 ? " bulto" : " bultos") + '</span><span class="small muted">Controlá con el cliente</span></div>' +
      '<div class="lines">' + shown.map(function (l) {
        return "<div><b>" + qty(l.qty) + "</b><span>" + h(l.product) + "</span></div>";
      }).join("") + "</div>" +
      (lines.length > 4 && !d.showAll ? '<button class="link" data-a="all-lines">Ver los ' + lines.length + " productos</button>" : "") +
      '<div class="sep"><div class="q">¿Devolvió algo? <small>Obligatorio</small></div><div class="grid2">' +
      '<button class="opt' + (d.returns_answer === "none" ? " on" : "") + '" data-a="ret-none">No, recibió todo</button>' +
      '<button class="opt' + (d.returns_answer === "yes" ? " on" : "") + '" data-a="ret-yes">Sí, devolvió</button></div>';
    if (d.returns_answer === "yes") {
      d.returns.forEach(function (r, i) {
        var line = lines.filter(function (l) { return l.id === r.line_id; })[0] || {};
        out += '<div class="ret"><div class="split"><span><b>' + qty(r.qty) + "</b> · " + h(line.product) + "</span><b>− " +
          money((line.unit_price || 0) * r.qty) + '</b></div><div class="split small muted"><span>Motivo: ' + h(r.reason) +
          '</span><button class="link red" data-a="ret-del" data-i="' + i + '">Quitar</button></div></div>';
      });
      if (d.adding) {
        out += '<div class="ret"><select class="inp" data-f="ret-line"><option value="">Elegí el producto</option>' +
          lines.map(function (l) {
            return '<option value="' + l.id + '"' + (d.retLine === l.id ? " selected" : "") + ">" + h(l.product) +
              " (" + qty(l.qty) + ")</option>";
          }).join("") + "</select>" +
          '<div class="grid2"><input class="inp" data-f="ret-qty" inputmode="decimal" placeholder="Cantidad" value="' +
          h(d.retQty || "") + '"><input class="inp" data-f="ret-reason" list="motivos" placeholder="Motivo" value="' +
          h(d.retReason || "") + '"></div><datalist id="motivos">' +
          REASONS.map(function (r) { return '<option value="' + h(r) + '">'; }).join("") + "</datalist>" +
          '<button class="opt second" data-a="ret-add">Agregar devolución</button></div>';
      } else {
        out += '<button class="link" data-a="ret-more">+ Otro producto devuelto</button>';
      }
    }
    out += "</div></div>";

    // receipt code
    out += '<div class="card strong' + (answered ? "" : " locked") + '">' +
      '<label class="field">Código de recibido<input class="inp code" data-f="code" inputmode="numeric" maxlength="4" ' +
      'autocomplete="off" value="' + h(d.code) + '"' +
      (answered && !d.without_code && !noCode && !(codeLocked(stop) && !d.code_ok) ? "" : " disabled") +
      ' aria-label="Código de recibido que dicta el cliente"></label>' +
      '<div id="code-status">' + codeStatus(stop) + "</div>" +
      '<div class="small muted">Obligatorio. Se habilita después de responder si devolvió algo, y acredita lo recibido ' +
      "menos lo devuelto. El cliente lo ve en la página de su pedido. Se valida sin señal.</div>" +
      // 19.0.1.129.22: never «disabled» (a disabled button swallows the tap):
      // before «¿Devolvió algo?» it says so
      '<button class="opt amberopt' + (d.without_code ? " on" : "") + '" data-a="no-code"' +
      (answered ? "" : ' aria-disabled="true"') + ">El cliente no tiene el código · Entregar igual</button>" +
      '<div class="small amber">Queda como "Entregado sin código": ante un reclamo por faltantes no hay constancia de recibido.</div></div>';

    // balance
    if (S.route.red_rule) {
      out += '<div class="card" style="gap:6px">' +
        '<div class="money-row"><span>Pedido de hoy</span><span>' + money(stop.order_amount) + "</span></div>" +
        (retAmount ? '<div class="money-row"><span>Devolución</span><span>− ' + money(retAmount) + "</span></div>" : "") +
        (red && red.debt > 0 ? '<div class="money-row"><span>Debe de días anteriores</span><span>' + money(red.debt) + "</span></div>" : "") +
        '<div class="money-row total"><span>Saldo a cobrar</span><span>' + money(toCollect(stop, d.returns)) + "</span></div></div>";
    } else {
      out += '<div class="card" style="gap:6px">' +
        '<div class="money-row"><span>Pedido de hoy</span><span>' + money(stop.order_amount) + "</span></div>" +
        (retAmount ? '<div class="money-row"><span>Devolución</span><span>− ' + money(retAmount) + "</span></div>" : "") +
        '<div class="money-row"><span>Deuda anterior</span><span>' + money(stop.previous_debt) + "</span></div>" +
        '<div class="money-row total"><span>Saldo total</span><span>' + money(balance(stop, d.returns)) + "</span></div></div>";
    }

    // payment
    out += '<div class="label">¿Cómo pagó?</div><div class="card" style="gap:12px">';
    // 19.0.1.129.22: the server says the order is already paid (a payment
    // without a receipt card too, e.g. the Mercado Pago report)
    if (stop.payments_paid) {
      out += '<div class="small pay-paid"><b>Ya está pagado</b>' +
        (stop.payments_paid_how ? " (" + h(stop.payments_paid_how) + ")" : "") +
        ": no cobres nada, tocá «Ya estaba pagado».</div>" +
        '<button class="opt teal' + (d.already_paid ? " on" : "") + '" data-a="already-paid">Ya estaba pagado</button>';
    } else if (stop.payments_to_confirm) {
      // 19.0.1.129.25: a receipt not credited yet covers what is due
      out += '<div class="small pay-confirm"><b>El cliente cargó un comprobante de ' +
        money(stop.payments_to_confirm_amount || 0) + "</b>, todavía sin acreditar, que cubre lo que debe (" +
        money(stop.payments_due || 0) + "). Si no te paga de nuevo, tocá «Ya estaba pagado · a confirmar»: " +
        "no cobrás nada y no queda a tu nombre. Administración lo confirma con Mercado Pago.</div>" +
        '<button class="opt teal' + (d.already_paid ? " on" : "") + '" data-a="already-paid">' + TO_CONFIRM_LABEL + "</button>";
    }
    out +=
      '<label class="field">Efectivo<input class="inp money" data-f="cash" inputmode="decimal" placeholder="$ 0" value="' +
      (d.cash ? h(money(d.cash)) : "") + '"></label>' +
      '<div class="stack" style="color:inherit"><div style="font-size:17px;font-weight:700">¿Paga con transferencia?</div><div class="grid2">' +
      '<button class="opt' + (d.transfer === "no" ? " on" : "") + '" data-a="tr" data-v="no">No</button>' +
      '<button class="opt teal' + (d.transfer === "yes" ? " on" : "") + '" data-a="tr" data-v="yes">Sí</button>' +
      "</div></div>";
    if (d.transfer === "yes") {
      out += '<label class="field">Importe transferido<input class="inp money" data-f="tamount" inputmode="decimal" placeholder="$ 0" value="' +
        (d.transfer_amount ? h(money(d.transfer_amount)) : "") + '"></label>' +
        '<div id="tr-short">' + shortHtml(stop, d) + "</div>";
      var company = S.route.company_whatsapp || "";
      var cwa = whatsappOf(stop);
      var ra = d.receipt_answer, hasPhoto = !!(d.thumb || d.receipt_photo);
      out += '<div class="card rcard" style="gap:10px"><div class="q">Comprobante <small>Obligatorio</small></div>' +
        '<div class="small">Sacale una foto al comprobante en el celular del cliente, o elegí cómo lo va a mandar.</div>' +
        '<div class="rchoices">' +
        // with a photo already taken the button only chooses it back
        (hasPhoto ? '<button class="opt teal photo-btn' + (ra === "photo" ? " on" : "") + '" data-a="rcpt" data-v="photo">' +
          ICON_CAMERA + "Foto o PDF del comprobante</button>"
          : '<label class="opt teal photo-btn' + (ra === "photo" ? " on" : "") + '">' + ICON_CAMERA +
          "Foto o PDF del comprobante" + RECEIPT_INPUT + "</label>") +
        '<button class="opt' + (ra === "customer_wa" ? " on" : "") + '" data-a="rcpt" data-v="customer_wa">El cliente lo manda por WhatsApp</button>' +
        '<button class="opt' + (ra === "not_yet" ? " on" : "") + '" data-a="rcpt" data-v="not_yet">Lo manda después</button>' +
        "</div>";
      if (ra === "photo") {
        out += '<div class="banner warn big-warn">' + ICON_CAMERA + "<span>" + h(RECEIPT_PHOTO_WARNING) + "</span></div>" +
          '<div class="photo">' + (d.thumb ? '<img src="' + d.thumb + '" alt="Foto del comprobante">'
            : '<span class="okline">' + ICON_CHECK + "Foto enviada</span>") +
          '<label class="opt second">Cambiar foto o PDF' + RECEIPT_INPUT + "</label></div>" +
          '<div class="small muted">La foto queda en el celular y se envía sola cuando hay señal. Administración la ' +
          "revisa; el pago se confirma con el reporte de Mercado Pago.</div>";
      } else if (ra === "customer_wa") {
        out += '<div class="pendbox"><b>Pendiente de comprobante · a tu cargo</b>' +
          "<span>Pedile que lo mande al WhatsApp de la empresa con el número de pedido " + h(stop.order) +
          ". Queda pendiente a tu nombre hasta que llegue.</span></div>";
      } else if (ra === "not_yet") {
        out += '<div class="pendbox"><b>Pendiente de comprobante · a tu cargo</b>' +
          "<span>Queda pendiente a tu nombre hasta que el comprobante llegue a la empresa.</span></div>";
      } else if (ra === "forwarded") {
        out += '<div class="small muted">Respuesta anterior: «Ya lo reenvié». Queda pendiente hasta que la ' +
          "empresa lo recibe; se resuelve solo.</div>";
      }
      // WhatsApp stays as the fallback
      out += (cwa.number ? '<a class="opt second" id="tr-claim" target="_blank" rel="noopener" href="' +
          h(waLink(waDigits(cwa.number), visitClaim(stop, d.transfer_amount || 0))) + '">Pedirle el comprobante por WhatsApp</a>' : "") +
        (company && ra !== "photo" ? '<a class="opt second" target="_blank" rel="noopener" href="' +
          h(waLink(company, "Comprobante del pedido " + stop.order)) + '">Si te lo mandó a vos: reenviar a la empresa</a>' : "") +
        "</div>";
    }
    out += '<label class="stack">Comentario (opcional)<input class="inp" data-f="comment" value="' + h(d.comment) +
      '" placeholder="Ej.: dice que transfirió desde la cuenta del hijo"></label>' +
      '<div class="note">El comprobante que no llegue a la empresa para la rendición de mañana se carga a tu cuenta, ' +
      "salvo que administración verifique el pago en Mercado Pago.</div></div>";

    // WhatsApp
    out += '<div class="card"><div class="stack"><span>WhatsApp del cliente, según el contacto</span>';
    if (d.waEditing) {
      out += '<input class="inp" data-f="wa" inputmode="tel" value="' + h(d.waNumber != null ? d.waNumber : wa.number) +
        '" placeholder="Ej.: 299 000-0000"></div><div class="grid2"><button class="opt primary" data-a="wa-save">Guardar número</button>' +
        '<button class="opt second" data-a="wa-cancel">Cancelar</button></div>';
    } else if (wa.number) {
      out += '<span class="phone">' + h(wa.number) + "</span>" +
        (wa.confirmed ? '<span class="okline">' + ICON_CHECK + "Confirmado</span>" : "") + "</div>" +
        '<div class="grid2"><button class="opt primary" data-a="wa-ok">Confirmar</button>' +
        '<button class="opt second" data-a="wa-edit">Corregir</button></div>';
    } else {
      out += '<span class="muted">El contacto no tiene número.</span></div>' +
        '<button class="opt primary" data-a="wa-edit">Cargar</button>';
    }
    out += "</div></div>";
    out += '<div class="foot"><button class="btn" data-a="save">Guardar visita</button></div>';
    // at the very end, after «Guardar visita» and small: easy to find,
    // hard to tap by mistake (both ask before losing what was entered)
    out += '<div class="alt"><div class="label">¿Algo salió distinto?</div><div class="grid2">' +
      '<button class="opt alt-opt" data-a="no-pay">No pagó nada</button>' +
      '<button class="opt alt-opt" data-a="nd">No se pudo entregar</button></div></div>';
    return out;
  }

  // «exigí el total»: what today's order still misses after cash and transfer
  function shortHtml(stop, d) {
    var short = round2(dueToday(stop, d.returns) - (d.cash || 0) - (d.transfer_amount || 0));
    return short > 1 ? '<div class="small red-text">Faltan ' + money(short) +
      " del pedido de hoy: exigí el total. La diferencia queda a tu cargo.</div>" : "";
  }
  // 19.0.1.129.22: an amount typed and then a tap elsewhere updates these in
  // place; a full re-render on «change» replaced the button under the finger
  // and the tap was lost
  function refreshTransferBits(stop) {
    if (!stop || !draft || draft.stop_id !== stop.id) return;
    var el = document.getElementById("tr-short");
    if (el) el.innerHTML = shortHtml(stop, draft);
    var link = document.getElementById("tr-claim"), cwa = whatsappOf(stop);
    if (link && cwa.number) link.href = waLink(waDigits(cwa.number), visitClaim(stop, draft.transfer_amount || 0));
  }

  // 19.0.1.99.0 (audit F12): wrong codes per stop, kept on the phone and
  // sent with the visit; at the server's limit the code locks
  function codeTries(stop) {
    var map = S.local.codeTries || {};
    return map[stop.id] || 0;
  }
  // the server's tries left already count what this stop synced
  function triesLeft(stop) {
    if (typeof stop.code_tries_left !== "number") return null;
    return stop.code_tries_left + (stop.code_attempts_recorded || 0) - codeTries(stop);
  }
  function codeLocked(stop) {
    if (stop.code_locked) return true;
    var left = triesLeft(stop);
    return left !== null && !!stop.code_hash && left <= 0;
  }
  function addCodeTry(stop) {
    S.local.codeTries = S.local.codeTries || {};
    S.local.codeTries[stop.id] = codeTries(stop) + 1;
    saveLocal();
  }

  function codeStatus(stop) {
    var d = draft;
    // 19.0.1.129.22: «Entregar igual» first, also when the code is locked or missing
    if (d.without_code) {
      return '<div class="small amber"><b>Entregado sin código</b> · ' +
        '<button class="link" data-a="no-code-undo">deshacer</button></div>';
    }
    if (codeLocked(stop) && !d.code_ok) {
      return '<div class="badline">Código bloqueado por intentos equivocados. Usá «Entregar igual» ' +
        "o pedile a administración que lo desbloquee y actualizá la ruta.</div>";
    }
    if (!stop.code_hash) {
      return '<div class="badline">Este pedido no tiene código cargado. Usá "Entregar igual".</div>';
    }
    if (d.code_ok) return '<div class="okline">' + ICON_CHECK + "Código correcto · Recibido acreditado</div>";
    if ((d.code || "").length === 4) {
      var left = triesLeft(stop);
      return '<div class="badline">Código incorrecto. Pedile que lo lea de nuevo' +
        (left !== null ? " (" + (left === 1 ? "queda 1 intento" : "quedan " + left + " intentos") + ")" : "") +
        ".</div>";
    }
    return "";
  }

  function checkCode(stop) {
    var code = draft.code || "";
    draft.code_ok = false;
    if (code.length !== 4 || !stop.code_hash || codeLocked(stop)) return Promise.resolve();
    var mine = draft;
    return sha256Hex(stop.code_salt + ":" + code).then(function (hex) {
      if (mine === draft && draft.code === code) {
        draft.code_ok = hex === stop.code_hash;
        if (!draft.code_ok) addCodeTry(stop);
        if (codeLocked(stop) && !draft.code_ok) {
          var input = document.querySelector('[data-f="code"]');
          if (input) input.disabled = true;
        }
        var el = document.getElementById("code-status");
        if (el) el.innerHTML = codeStatus(stop);
      }
    });
  }

  function visitPayload(d) {
    return {
      visit_uuid: d.visit_uuid, stop_id: d.stop_id, outcome: d.outcome, reason: d.reason || "",
      returns_answer: d.outcome === "delivered" ? d.returns_answer : null,
      returns: d.returns_answer === "yes" ? d.returns : [],
      code: d.code_ok ? d.code : "", without_code: !!d.without_code,
      code_attempts: d.code_attempts || 0,
      // a visit restored from the server keeps its validated code
      code_already_ok: !!(d.code_ok && !d.code),
      cash: d.cash || 0, transfer: d.transfer || "no",
      transfer_amount: d.transfer === "yes" ? d.transfer_amount || 0 : 0,
      receipt_answer: d.transfer === "yes" ? d.receipt_answer : null,
      no_payment: !!d.no_payment, comment: d.comment || "",
      // 19.0.1.129.22: «Ya estaba pagado» (older servers ignore it)
      already_paid: !!d.already_paid,
      // 19.0.1.129.25: «a confirmar» (informs only: the server decides)
      to_confirm: !!(d.already_paid && d.to_confirm),
    };
  }

  function storeVisit(stop, d, gps) {
    var photoAnswer = d.transfer === "yes" && d.receipt_answer === "photo";
    var visit = {
      visit_uuid: d.visit_uuid, stop_id: stop.id, outcome: d.outcome, reason: d.reason || "",
      returns_answer: d.returns_answer, returns: d.returns, code: d.code_ok ? d.code : "", code_ok: !!d.code_ok,
      code_attempts: codeTries(stop),
      without_code: !!d.without_code, cash: d.cash || 0, transfer: d.transfer || "no",
      transfer_amount: d.transfer === "yes" ? d.transfer_amount || 0 : 0,
      receipt_answer: d.transfer === "yes" ? d.receipt_answer : null, red_ok: !!d.red_ok,
      no_payment: !!d.no_payment, already_paid: !!d.already_paid, to_confirm: !!(d.already_paid && d.to_confirm),
      comment: d.comment || "", saved_at: new Date().toISOString(), synced: false, error: null,
      // only the thumbnail: the photo itself lives in its outbox event
      thumb: photoAnswer ? d.thumb || null : null, photo_uuid: photoAnswer ? d.photo_uuid || null : null,
      receipt_photo: !!d.receipt_photo, photo_error: d.photo ? null : d.photo_error || null,
    };
    S.local.visits[stop.id] = visit;
    var payload = visitPayload(visit);
    if (gps !== undefined) payload.gps = gps;
    return saveLocal().then(function () {
      return enqueue("visit", payload, "visit:" + stop.id);
    }).then(function () {
      // the photo after its visit, in its own event: a retake before it
      // left the phone replaces it; another answer drops it
      if (photoAnswer && d.photo) {
        var receipt = { photo_uuid: d.photo_uuid, stop_id: stop.id, visit_uuid: d.visit_uuid,
                        transfer_amount: d.transfer_amount || 0, photo: d.photo };
        d.photo = null;
        return enqueue("receipt", receipt, "receipt:" + stop.id);
      }
      if (!photoAnswer) return dropQueued("receipt:" + stop.id);
    });
  }

  function validateDelivered(stop, d) {
    if (d.returns_answer !== "none" && d.returns_answer !== "yes") return "Respondé si devolvió algo.";
    if (d.returns_answer === "yes" && !d.returns.length) return "Cargá lo que devolvió, o respondé «No, recibió todo».";
    if (!d.code_ok && !d.without_code) return "Falta el código de recibido, o tocá «Entregar igual».";
    if (redOf(stop) && !d.red_ok) {
      return "Cliente en rojo: cobrá lo que debe y reenviá los comprobantes antes de descargar, o guardá «No se pudo entregar».";
    }
    return "";
  }

  // --- parcel stop: dispatched at the bus terminal

  function newParcelDraft(stop) {
    var v = visitOf(stop);
    if (v) {
      var d = JSON.parse(JSON.stringify(v));
      // an edit before the photo reached the server keeps that photo
      var pending = S.outbox.filter(function (e) { return e.key === "visit:" + stop.id; })[0];
      d.photo = pending && pending.payload.photo || null;
      return d;
    }
    return { visit_uuid: uuid(), stop_id: stop.id, outcome: "dispatched", reason: "", guide_number: "",
             cost: 0, packages: stop.parcels || 1, comment: "", photo: null, thumb: null, has_photo: false };
  }

  function viewParcel(stop) {
    if (!draft || draft.stop_id !== stop.id) draft = newParcelDraft(stop);
    var d = draft, p = stop.parcel, pays = p.payer === "distri";
    var out = '<div class="head"><a class="back" href="#visitas">‹ Visitas</a><h1>' + h(stop.name) + "</h1>" +
      '<div class="sub">' + h([p.remito || stop.order, (stop.parcels || 0) + (stop.parcels === 1 ? " bulto" : " bultos")]
        .join(" · ")) + "</div></div>" +
      '<div id="status">' + statusBanner() + "</div>" +
      packedHtml(stop) +
      '<div class="parcel-hero"><div class="k">Encomienda · Terminal de ómnibus</div>' +
      '<div class="dest">Destino: terminal de <b>' + h(p.town) + "</b></div>" +
      '<div class="carrier">Transportista: <b>' + h(p.carrier || "a elegir en la terminal") + "</b></div>" +
      '<div class="payer-big ' + (pays ? "distri" : "customer") + '">' + h(p.payer_label) + "</div>" +
      (p.recipient ? '<div class="recipient"><span>Retira en destino</span><b>' + h(p.recipient) + "</b>" +
        (p.dni ? "<b>DNI " + h(p.dni) + "</b>" : "") + "</div>" : "") +
      "</div>" + '<div class="body">';
    if (d.outcome === "not_delivered") {
      out += '<div class="label">No se pudo despachar</div><div class="card">' +
        '<label class="stack">¿Por qué no se despachó? (obligatorio al cerrar la ruta)' +
        '<input class="inp" data-f="reason" value="' + h(d.reason) + '" placeholder="Ej.: la boletería estaba cerrada"></label>' +
        '<div class="small muted">Los ' + (stop.parcels || 0) + " bultos vuelven al depósito.</div>" +
        '<button class="link" data-a="undo-nd">Volver: sí se despachó</button></div></div>' +
        '<div class="foot"><button class="btn" data-a="parcel-nd-save">Guardar: no se despachó</button></div>';
      return out;
    }
    out += '<div class="note">El cliente no está: no se pide el código de recibido. Dictale al transportista ' +
      "el destino, quién retira y quién paga el envío.</div>" +
      '<div class="label">Despachar encomienda</div><div class="card" style="gap:12px">' +
      '<div class="q">Foto de la guía o ticket del transportista <small>Obligatoria</small></div>' +
      '<div class="card warn photo-warn">La foto tiene que mostrar el número de guía' +
      (pays ? " y el importe" : "") + ", legibles." + (pays ? " El ticket en papel se entrega en la rendición." : "") + "</div>" +
      photoField(d, d.has_photo && !d.thumb ? "Foto enviada · Sacar otra" : "Sacar foto de la guía") +
      '<label class="stack">Número de guía<input class="inp" data-f="guide" inputmode="text" autocomplete="off" value="' +
      h(d.guide_number) + '" placeholder="Si no se lee en la foto"></label>';
    if (pays) {
      out += '<label class="field">Costo del envío<input class="inp money" data-f="pcost" inputmode="decimal" placeholder="$ 0" value="' +
        (d.cost ? h(money(d.cost)) : "") + '"></label>' +
        '<div class="small muted">Pagado con el efectivo del día: se descuenta del efectivo a rendir como gasto «Encomienda».</div>';
    }
    out += '<label class="field">Bultos despachados<input class="inp bultos" data-f="packages" inputmode="numeric" value="' +
      h(d.packages || "") + '"></label>' +
      '<label class="stack">Comentario (opcional)<input class="inp" data-f="comment" value="' + h(d.comment) + '"></label>' +
      "</div>" +
      '<div class="card"><div class="lines">' + stop.lines.slice(0, d.showAll ? stop.lines.length : 4).map(function (l) {
        return "<div><b>" + qty(l.qty) + "</b><span>" + h(l.product) + "</span></div>";
      }).join("") + "</div>" +
      (stop.lines.length > 4 && !d.showAll ? '<button class="link" data-a="all-lines">Ver los ' + stop.lines.length + " productos</button>" : "") +
      "</div></div>" +
      '<div class="foot"><button class="btn" data-a="parcel-save">Despachar encomienda</button>' +
      '<div class="hint">La ruta no se cierra sin la foto de la guía.</div></div>' +
      '<div class="alt"><div class="label">¿Algo salió distinto?</div>' +
      '<button class="opt alt-opt" data-a="nd">No se pudo despachar</button></div>';
    return out;
  }

  function storeParcel(stop, d, gps) {
    var visit = {
      visit_uuid: d.visit_uuid, stop_id: stop.id, outcome: d.outcome, reason: d.reason || "",
      guide_number: (d.guide_number || "").trim(), cost: d.cost || 0, packages: d.packages || 0,
      comment: d.comment || "", thumb: d.thumb || null, has_photo: !!d.has_photo,
      saved_at: new Date().toISOString(), synced: false, error: null,
    };
    S.local.visits[stop.id] = visit;
    var payload = {
      visit_uuid: visit.visit_uuid, stop_id: stop.id, outcome: visit.outcome, reason: visit.reason,
      guide_number: visit.guide_number, cost: visit.cost, packages: visit.packages, comment: visit.comment,
    };
    if (d.photo) payload.photo = d.photo;
    if (gps !== undefined) payload.gps = gps;
    return saveLocal().then(function () { return enqueue("parcel", payload, "visit:" + stop.id); });
  }

  // --- expenses («Cargar gasto»)

  function viewExpenses(fromClose) {
    if (!expDraft) expDraft = { kind: null, amount: 0, comment: "", photo: null, thumb: null };
    var d = expDraft, items = expenseItems();
    var r = closeSummary();
    var out = '<div class="head"><a class="back" href="' + (fromClose ? "#cierre" : "#visitas") + '">‹ ' +
      (fromClose ? "Cerrar la ruta" : "Visitas") + "</a><h1>Cargar gasto</h1>" +
      '<div class="sub">Se paga con el efectivo del día</div></div>' +
      '<div id="status">' + statusBanner() + "</div>" +
      '<div class="banner warn big-warn">' + ICON_CAMERA + "<span>" + h(PHOTO_WARNING) + "</span></div>" +
      '<div class="body"><div class="card" style="gap:12px"><div class="q">Tipo de gasto</div><div class="grid3 kinds">' +
      EXPENSE_KINDS.map(function (k) {
        return '<button class="opt' + (d.kind === k[0] ? " on" : "") + '" data-a="exp-kind" data-v="' + k[0] + '">' + k[1] + "</button>";
      }).join("") + "</div>" +
      '<label class="field">Importe<input class="inp money" data-f="exp-amount" inputmode="decimal" placeholder="$ 0" value="' +
      (d.amount ? h(money(d.amount)) : "") + '"></label>' +
      '<label class="stack">Comentario (opcional)<input class="inp" data-f="exp-comment" value="' + h(d.comment) +
      '" placeholder="Ej.: YPF de Ruta 22"></label>' +
      '<div class="q">Foto del comprobante <small>Obligatoria</small></div>' +
      photoField(d, "Sacar foto del comprobante") +
      '<button class="btn" data-a="exp-save">Guardar gasto</button></div>';
    out += '<div class="label">Gastos de la ruta</div>';
    if (!items.length) {
      out += '<div class="card muted">Todavía no hay gastos cargados.</div>';
    } else {
      out += items.map(function (e) {
        return '<div class="card exp"><div class="split">' +
          (e.thumb ? '<img class="thumb" src="' + e.thumb + '" alt="">' : "") +
          '<span class="t"><b>' + h(kindLabel(e.kind)) + "</b><span>" + h(e.comment || "") + "</span>" +
          '<span class="small ' + (e.error ? "badline" : "muted") + '">' +
          h(e.error ? "No se envió: " + e.error : e.synced ? "Enviado" : "Guardado en el celular") + "</span></span>" +
          "<b>− " + money(e.amount) + "</b></div>" +
          (e.own ? '<button class="link red" data-a="exp-del" data-id="' + h(e.uuid) + '">Anular</button>' :
            '<span class="small muted">Del despacho de la encomienda</span>') + "</div>";
      }).join("");
    }
    out += cashCard(r) + "</div>";
    return out;
  }

  function cashCard(r) {
    return '<div class="card cashbox"><div class="money-row"><span>Efectivo cobrado</span><span>' + money(r.cash) + "</span></div>" +
      (r.advance ? '<div class="money-row"><span>Adelanto para la ruta</span><span>+ ' + money(r.advance) + "</span></div>" : "") +
      '<div class="money-row"><span>Gastos</span><span>− ' + money(r.expenses) + "</span></div>" +
      '<div class="money-row total"><span>Efectivo a rendir</span><span>' + money(r.toSettle) + "</span></div></div>";
  }

  // --- customer: location and hours

  function viewCustomer(stop) {
    if (!custDraft || custDraft.stop_id !== stop.id) {
      var c = customerOf(stop);
      var hours = String(c.hours || "").split(";").filter(Boolean).map(function (r) { return r.split("-"); });
      custDraft = {
        stop_id: stop.id, lat: c.lat, lng: c.lng, location: null,
        location_by: c.location_by || "", location_date: c.location_date || "",
        days: String(c.days || "").split(",").filter(function (x) { return x !== ""; }).map(Number),
        hours: hours.length ? hours : [["", ""]], note: c.note || "", distance: null,
      };
      locateDistance(stop);
    }
    var d = custDraft;
    var has = d.lat != null && d.lng != null;
    var saved = has ? (d.location === "gps" ? "Ubicación nueva, sin guardar todavía" :
      (d.location_by ? "Guardada por " + d.location_by + (d.location_date ? " el " + shortDate(d.location_date) : "") :
        "Guardada")) : "Sin ubicación guardada";
    if (d.distance != null && has && d.location !== "gps") saved += " · Estás a " + fmtDistance(d.distance);
    if (d.location === "confirm") saved += " · Confirmada";
    var map = has ? '<a class="map" target="_blank" rel="noopener" href="https://www.google.com/maps?q=' + d.lat + "," + d.lng +
      '"><b>' + ICON_PIN + "</b><span>" + Number(d.lat).toFixed(5) + ", " + Number(d.lng).toFixed(5) +
      "</span><span>Abrir en el mapa</span></a>" : '<div class="map">Todavía no hay ubicación exacta.</div>';
    // sent by the customer from the order link: never replaces the one above
    var sent = stop.customer || {};
    var sentLoc = sent.customer_lat != null && sent.customer_lng != null ?
      '<a class="cust-loc" target="_blank" rel="noopener" href="https://www.google.com/maps?q=' + sent.customer_lat + "," +
      sent.customer_lng + '"><b>Ubicación enviada por el cliente · Abrir en el mapa</b><span>' +
      h(["El " + shortDate(sent.customer_location_date), sent.customer_accuracy ? "precisión ±" + sent.customer_accuracy + " m" : ""]
        .filter(Boolean).join(" · ")) + "</span></a>" : "";
    var sentHours = sent.hours_by_customer_date ?
      '<div class="small muted">El cliente cambió los horarios el ' + h(shortDate(sent.hours_by_customer_date)) + ".</div>" : "";
    var out = '<div class="head"><a class="back" href="#visita/' + stop.id + '">‹ Visita</a><h1>Ubicación y horarios</h1>' +
      '<div class="sub">' + h([stop.name, stop.address].filter(Boolean).join(" · ")) + "</div></div>" +
      '<div id="status">' + statusBanner() + "</div>" +
      '<div class="body"><div class="label">Ubicación exacta</div>' +
      '<div class="card" style="padding:0;overflow:hidden;gap:0">' + map +
      '<div style="padding:12px 14px;display:flex;flex-direction:column;gap:10px"><div class="small muted">' + h(saved) +
      '</div><div class="grid2"><button class="opt primary" data-a="loc-ok"' + (has ? "" : " disabled") +
      ">Confirmar ubicación</button>" +
      '<button class="opt second" data-a="loc-gps">Usar dónde estoy</button></div></div>' + sentLoc + "</div>" +
      '<div class="label">Horarios en que recibe</div><div class="card" style="gap:12px">' + sentHours + '<div class="grid7">' +
      DAYS.map(function (name, i) {
        return '<button class="opt day' + (d.days.indexOf(i) >= 0 ? " on" : "") + '" data-a="day" data-v="' + i + '">' +
          name + "</button>";
      }).join("") + "</div>" +
      d.hours.map(function (r, i) {
        var label = i === 0 ? "Mañana" : i === 1 ? "Tarde" : "Horario";
        return '<div class="hours"><span class="l">' + label + '</span><input class="inp time" type="time" data-f="h-from" data-i="' +
          i + '" value="' + h(r[0]) + '" aria-label="Desde"><span>a</span><input class="inp time" type="time" data-f="h-to" data-i="' +
          i + '" value="' + h(r[1]) + '" aria-label="Hasta">' +
          (d.hours.length > 1 ? '<button class="link red" data-a="h-del" data-i="' + i + '">✕</button>' : "") + "</div>";
      }).join("") +
      (d.hours.length < 4 ? '<button class="link" data-a="h-add">+ Agregar horario</button>' : "") + "</div>" +
      // 19.0.1.129.33: app5.js adds the customer's WhatsApp here («Reparto v2» only)
      (extHook("customerHtml", stop) || "") +
      '<label class="stack"><span class="label" style="padding:0">Comentario para los repartidores</span>' +
      '<textarea class="inp" rows="4" data-f="note" placeholder="Portón, por dónde descargar, quién atiende">' + h(d.note) +
      "</textarea></label></div>" +
      '<div class="foot"><button class="btn" data-a="cust-save">Guardar datos del cliente</button>' +
      '<div class="hint">Se guarda en el celular y se envía al sincronizar. Lo ven todos los repartidores.</div></div>';
    return out;
  }

  function shortDate(text) {
    var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(text || "");
    return m ? m[3] + "/" + m[2] : "";
  }
  function fmtDistance(m) { return m < 1000 ? Math.round(m) + " m" : (m / 1000).toFixed(1).replace(".", ",") + " km"; }
  function distance(a, b, c, d) {
    var R = 6371000, toRad = Math.PI / 180;
    var dLat = (c - a) * toRad, dLng = (d - b) * toRad;
    var x = Math.sin(dLat / 2) * Math.sin(dLat / 2) +
      Math.cos(a * toRad) * Math.cos(c * toRad) * Math.sin(dLng / 2) * Math.sin(dLng / 2);
    return 2 * R * Math.asin(Math.sqrt(x));
  }
  function locateDistance(stop) {
    var d = custDraft;
    if (!navigator.geolocation || d.lat == null) return;
    navigator.geolocation.getCurrentPosition(function (pos) {
      if (custDraft !== d) return;
      d.distance = distance(d.lat, d.lng, pos.coords.latitude, pos.coords.longitude);
      render.keepScroll = true; render();
    }, function () {}, { enableHighAccuracy: true, timeout: 15000, maximumAge: 60000 });
  }

  // --- close the route

  function closeSummary() {
    var list = stops(), sum = { paid: 0, not_paid: 0, already_paid: 0, not_delivered: 0, pending: 0, dispatched: 0 };
    var cash = 0, transfers = 0, noCode = 0, unsynced = 0, noGuide = [], toConfirm = 0;
    list.forEach(function (s) {
      var st = stopState(s), v = visitOf(s);
      sum[st] += 1;
      if (!v) return;
      if (st === "already_paid" && v.to_confirm) toConfirm += 1;
      cash += v.cash || 0;
      if (v.transfer === "yes") transfers += 1;
      if (v.outcome === "delivered" && v.without_code) noCode += 1;
      if (v.outcome === "dispatched" && !v.thumb && !v.has_photo) noGuide.push(s);
      // 19.0.1.129.22: only while the server keeps its rejection open (a
      // rejection resolved or resent there no longer blocks the close)
      if (v.error && visitRejection(S.local, S.route.rejections, s, v) !== "closed") unsynced += 1;
    });
    var expenses = expensesTotal();
    unsynced += expenseItems().filter(function (e) { return e.own && e.error; }).length;
    // 19.0.1.129.32: the advance for the route goes back with the cash
    var advance = Number(S.route && S.route.advance_total) || 0;
    return { sum: sum, cash: round2(cash), transfers: transfers, noCode: noCode, advance: advance,
             unsynced: unsynced, total: list.length, expenses: expenses,
             toSettle: round2(cash + advance - expenses),
             noGuide: noGuide, toConfirm: toConfirm };
  }

  // «Transferencias sin comprobante · a tu cargo»: what the server knows
  // (pending, expired and resolved today, of every route of the driver)
  // plus today's transfers of this route not synced yet
  function receiptItems() {
    var byStop = {}, items = [];
    (S.route.receipts || []).forEach(function (r) { byStop[r.stop_id] = r; items.push(r); });
    stops().forEach(function (s) {
      var v = visitOf(s);
      if (!v || v.outcome !== "delivered") return;
      var unpaid = stopState(s) === "not_paid";
      if (v.transfer !== "yes" && !unpaid) return;
      var amount = unpaid ? dueToday(s, v.returns) : v.transfer_amount || 0;
      if (!(amount > 0)) return;
      var known = byStop[s.id];
      if (known) {
        if (known.state !== "fulfilled") { known.amount = amount; known.kind = unpaid ? "unpaid" : "done"; }
        return;
      }
      items.push({ stop_id: s.id, partner_id: s.commercial_partner_id, order: s.order, document: s.document,
                   name: s.name, amount: amount, kind: unpaid ? "unpaid" : "done",
                   delivered_at: v.saved_at || new Date().toISOString(),
                   state: "open", overdue_since: "", resolved_by: "", whatsapp: waDigits(whatsappOf(s).number) });
    });
    return items;
  }

  function receiptsHtml() {
    var items = receiptItems();
    if (!items.length) return "";
    var owed = items.filter(function (r) { return r.state === "open" || r.state === "overdue"; });
    var total = round2(owed.reduce(function (sum, r) { return sum + (r.amount || 0); }, 0));
    // 19.0.1.99.0 (audit F15): what is still owed first, under «A tu cargo»;
    // what already arrived apart («Resueltas»), and a photo saved on the
    // phone says so instead of «Pendiente»
    var resolved = items.filter(function (r) { return r.state === "fulfilled"; });
    items = items.filter(function (r) { return r.state !== "fulfilled"; }).concat(resolved);
    var out = owed.length ? '<div class="label">A tu cargo · transferencias sin comprobante y pedidos sin cobrar</div>' : "";
    items.forEach(function (r, n) {
      var done = r.state === "fulfilled", late = r.state === "overdue";
      if (done && (n === 0 || items[n - 1].state !== "fulfilled")) {
        out += '<div class="label">Resueltas · ya no están a tu cargo</div>';
      }
      var stop = stopById(r.stop_id), v = stop && visitOf(stop);
      var photoWaiting = !done && v && v.receipt_answer === "photo" &&
        S.outbox.some(function (e) { return e.key === "receipt:" + stop.id; });
      var pill = done ? '<span class="pill paid">Llegó el comprobante' + (r.resolved_by ? " · resuelta por " + h(r.resolved_by) : "") + "</span>" :
        late ? '<span class="pill not_paid">Vencida desde ' + h(r.overdue_since) + "</span>" :
        photoWaiting ? '<span class="pill pendpill">Foto guardada en el celular · se envía sola</span>' :
        '<span class="pill pendpill">Pendiente</span>';
      out += '<div class="card rcpt' + (late ? " late" : "") + (done ? " done" : "") + '" style="gap:6px">' +
        '<div class="split"><b>' + h(r.name) + '</b><b class="' + (done ? "strike" : "") + '">' + money(r.amount) + "</b></div>" +
        '<div class="small"><b>' + h(kindLabel2(r.kind)) + "</b></div>" +
        '<div class="small">' + h(done ? (r.document || r.order) + " · entregado " + relWhen(r.delivered_at) : itemLine(r)) + "</div>" + pill +
        (!done && r.whatsapp ? '<a class="opt second" target="_blank" rel="noopener" href="' +
          h(waLink(r.whatsapp, customerClaim(items.filter(function (x) { return x.partner_id === r.partner_id; })))) +
          '">Reclamar por WhatsApp</a>' : "") + "</div>";
    });
    if (owed.length) {
      out += '<div class="owed"><span>Pendiente a tu cargo · ' + owed.length + "</span><b>" + money(total) + "</b></div>";
    }
    out += '<div class="note">Las pendientes de hoy vencen en la rendición de mañana. Si para entonces no llegó el ' +
      "comprobante o el pago, el importe se carga a tu cuenta.</div>";
    return out;
  }

  function viewClose() {
    var r = closeSummary(), list = stops();
    var withState = r.total - r.sum.pending;
    var nd = list.filter(function (s) { return stopState(s) === "not_delivered"; });
    var out = '<div class="head"><a class="back" href="#visitas">‹ Visitas</a><h1>Cerrar la ruta</h1>' +
      '<div class="sub">' + withState + " de " + r.total + " visitas con estado</div></div>" +
      '<div id="status">' + statusBanner() + "</div>" +
      '<div class="body"><div class="card" style="gap:2px">' +
      '<div class="sum"><span>Pagaron (efectivo o transferencia)</span><b>' + r.sum.paid + "</b></div>" +
      '<div class="sum"><span>No pagaron</span><b>' + r.sum.not_paid + "</b></div>" +
      (r.sum.already_paid ? '<div class="sum"><span>Ya estaba pagado' +
        (r.toConfirm ? " (" + r.toConfirm + " a confirmar)" : "") + "</span><b>" + r.sum.already_paid + "</b></div>" : "") +
      '<div class="sum"><span>No se entregó</span><b>' + r.sum.not_delivered + "</b></div>" +
      (r.sum.dispatched ? '<div class="sum"><span>Encomiendas despachadas</span><b>' + r.sum.dispatched + "</b></div>" : "") +
      (r.sum.pending ? '<div class="sum" style="color:var(--red)"><span>Sin estado</span><b>' + r.sum.pending + "</b></div>" : "") +
      '<div class="sum amber"><span>Entregados sin código de recibido</span><b>' + r.noCode + "</b></div></div>";
    nd.forEach(function (s) {
      var v = visitOf(s);
      out += '<div class="card warn" style="gap:8px"><div class="q">No se entregó · ' + h(s.name) + "</div>" +
        '<div class="small">' + h(s.order) + " · " + (s.parcels || 0) + (s.parcels === 1 ? " bulto vuelve" : " bultos vuelven") +
        ' al depósito</div><label class="stack" style="color:var(--text)">¿Por qué no se entregó? (obligatorio)' +
        '<input class="inp" data-f="nd-reason" data-id="' + s.id + '" value="' + h(v.reason) + '"></label></div>';
    });
    out += '<div class="card"><div class="muted">Efectivo para rendir en el depósito</div><div class="cash">' + money(r.toSettle) +
      "</div>" + '<div class="money-row"><span>Efectivo cobrado</span><span>' + money(r.cash) + "</span></div>" +
      (r.advance ? '<div class="money-row"><span>Adelanto para la ruta</span><span>+ ' + money(r.advance) + "</span></div>" : "") +
      '<div class="money-row"><span>Gastos</span><span>− ' + money(r.expenses) + "</span></div>" +
      '<div class="small">Efectivo cobrado − gastos = efectivo a rendir. Los comprobantes en papel de los gastos ' +
      "se entregan con el efectivo.</div>" +
      '<a class="opt second" href="#gasto/cierre">Cargar gasto</a>' +
      '<div class="small muted">Al volver hoy o mañana a primera hora</div>' +
      "</div>" + receiptsHtml() + "</div>";
    out += '<div class="foot"><button class="btn" data-a="close">Cerrar la ruta</button>' +
      '<div class="hint">No se puede cerrar con visitas sin estado, sin el motivo de lo no entregado, sin la foto de la guía de ' +
      "una encomienda ni con cambios sin enviar.</div></div>";
    return out;
  }

  // «Tus rutas de hoy»: the driver's open routes, the one «En reparto» first
  // (the server's order). Opening one never settles or closes the other.
  function viewRoutes() {
    var cards = (S.otherRoutes || []).map(function (r) {
      var on = S.route && r.id === S.route.id, n = pendingFor(r.id);
      var bits = [r.detail || ""];
      if (n) bits.push(n + (n === 1 ? " cambio sin enviar" : " cambios sin enviar"));
      if (on) bits.push("es la que tenés abierta");
      return '<button class="visit route-pick' + (on ? " next" : "") + '" data-a="open-route" data-id="' + r.id + '">' +
        '<div class="split"><span class="t">' + h(r.title || r.name) + '</span><span class="pill ' +
        (r.state === "on_route" ? "paid" : "pendpill") + '">' + h(r.state_label || "") + "</span></div>" +
        '<div class="d">' + h(bits.filter(Boolean).join(" · ")) + "</div></button>";
    }).join("");
    return '<div class="head">' + (S.route ? '<a class="back" href="#">‹ Volver</a>' : "") +
      "<h1>Tus rutas de hoy</h1>" + '<div class="sub">' + h(S.user) + "</div></div>" +
      '<div id="status">' + statusBanner() + "</div>" +
      '<div class="body">' + cards +
      '<div class="card small muted">Podés abrir otra ruta sin rendir la primera: cada una guarda sus visitas ' +
      "y sus cambios en el celular hasta que se envían.</div></div>";
  }

  function viewClosed() {
    var r = closeSummary();
    var next = S.otherRoutes.filter(function (x) { return x.id !== S.route.id; });
    return '<div class="head"><div class="kicker">' + h(S.route.name) + "</div><h1>Ruta cerrada</h1></div>" +
      '<div id="status">' + statusBanner() + "</div>" + routesBar() + (extHook("homeHtml") || "") +
      '<div class="body"><div class="card"><div class="muted">Efectivo para rendir en el depósito</div><div class="cash">' +
      money(r.toSettle) + "</div>" +
      (r.expenses || r.advance ? '<div class="small">Efectivo cobrado ' + money(r.cash) +
        (r.advance ? " + adelanto " + money(r.advance) : "") + " − gastos " + money(r.expenses) + "</div>" : "") +
      '<div class="small muted">Al volver hoy o mañana a primera hora</div></div>' +
      (canReopen() ? '<div class="card"><div class="small muted">¿Te equivocaste en una visita? Reabrí la ruta, ' +
        "corregila y cerrala de nuevo. Se puede hasta que caja cuente el efectivo.</div>" +
        '<button class="btn outline" data-a="reopen-route">Reabrir la ruta</button></div>' : "") + "</div>" +
      '<div class="foot"><button class="btn" data-a="next-route">' +
      (next.length ? "Descargar la próxima ruta" : "Buscar otra ruta") + "</button></div>";
  }

  // 19.0.1.129.25: «Reabrir la ruta», only once its close reached the server
  // and while caja has not counted the cash (the server decides anyway)
  function canReopen() {
    return !!(S.route && S.local && S.local.closed && !pendingFor(S.route.id) && !S.route.reopen_locked &&
              S.route.state !== "settled");
  }

  function reopenRoute() {
    var routeId = S.route.id;
    toast("Reabriendo la ruta…");
    return fetchWithTimeout(API_REOPEN, { method: "POST", credentials: "same-origin", cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ route_id: routeId, user_id: S.uid }) }, 20000)
      .then(function (res) {
        if (isLoginPage(res)) { S.auth = false; throw new Error("Tu sesión venció. Entrá de nuevo con tu usuario."); }
        return res.json();
      }).then(function (data) {
        if (!data || !data.ok) {
          if (data && data.locked && S.route && S.route.id === routeId) {
            S.route.reopen_locked = true;
            saveRoute();
          }
          throw new Error((data && data.error) || "No se pudo reabrir la ruta.");
        }
        wanted = routeId;
        return download(false).then(function () {
          if (S.route && S.route.id === routeId && !S.local.closed) {
            toast("Ruta reabierta: corregí la visita y cerrala de nuevo.");
            go("#visitas");
          }
        });
      }).catch(function (e) {
        toast(e && e.name === "TypeError" || (e && e.name === "AbortError") ?
          "Sin señal: para reabrir la ruta hace falta señal." : e.message, true);
        render();
      });
  }

  // the poll (app4.js) brings the route's state: reopened by someone else
  // while «Ruta cerrada» is on screen, download it again
  function onRouteState(state) {
    if (state === "on_route" && S.route && S.local && S.local.closed && !pendingFor(S.route.id)) {
      wanted = S.route.id;
      download(false);
    }
  }

  // ------------------------------------------------------------- events

  document.addEventListener("click", function (ev) {
    var el = ev.target.closest("[data-a]");
    if (!el || el.disabled) return;
    var a = el.getAttribute("data-a");
    var parts = location.hash.replace(/^#/, "").split("/");
    var stop = stopById(parts[1]);
    ev.preventDefault();
    var handler = ACTIONS[a];
    if (handler) handler(el, stop);
  });

  function rerender() { render.keepScroll = true; render(); }

  var ACTIONS = {
    download: function () { download(true); },
    "acct-reload": function (el, stop) { if (stop) { loadAccount(stop); rerender(); } },
    // «Entendido» only hides the banner: the rejected changes stay in
    // «Cambios rechazados» until they are resent or resolved
    dismiss: function () {
      openRejections().forEach(function (x) { x.hidden = true; });
      saveLocal().then(render);
    },
    "rej-resend": function (el) {
      var root = el.getAttribute("data-id");
      var item = openRejections().filter(function (x) { return x.root === root; })[0];
      if (!item) return;
      var ev = item.event;
      item.hidden = true;
      saveLocal().then(function () { return enqueue(ev.type, ev.payload, ev.key, root); })
        .then(function () { toast("Reenviado. Si el servidor lo acepta, desaparece de la lista."); rerender(); });
    },
    load: function (el) {
      if (S.route.controlled) return toast("La carga ya fue controlada con Administración.", true);
      var id = +el.getAttribute("data-id");
      var on = !S.local.loaded[id];
      S.local.loaded[id] = on;
      // the loading order, to offer the visits in reverse when leaving
      S.local.loadOrder = (S.local.loadOrder || []).filter(function (x) { return x !== id; });
      if (on) S.local.loadOrder.push(id);
      S.departError = null;
      saveLocal().then(function () { return enqueue("load", { stop_id: id, loaded: on }, "load:" + id); });
      rerender();
    },
    depart: function () {
      // the ticks first, so the proposed order is the one the server applies
      S.departError = null;
      syncNow().then(pollLoad).then(function () { go("#salida"); });
    },
    "depart-go": function (el) {
      var reverse = el.getAttribute("data-v") === "1";
      withGps("start", "salir a repartir", function (gps) { departNow(reverse, gps); });
    },
    "all-lines": function () { draft.showAll = true; rerender(); },
    "ret-none": function () { draft.returns_answer = "none"; rerender(); },
    "ret-yes": function () { draft.returns_answer = "yes"; draft.adding = !draft.returns.length; rerender(); },
    "ret-more": function () { draft.adding = true; rerender(); },
    "ret-del": function (el) {
      draft.returns.splice(+el.getAttribute("data-i"), 1);
      if (!draft.returns.length) draft.adding = true;
      rerender();
    },
    "ret-add": function (el, stop) {
      var line = stop.lines.filter(function (l) { return l.id === draft.retLine; })[0];
      var q = parseMoney(draft.retQty);
      if (!line) return toast("Elegí el producto devuelto.", true);
      var already = draft.returns.filter(function (r) { return r.line_id === line.id; })
        .reduce(function (s, r) { return s + r.qty; }, 0);
      if (!(q > 0)) return toast("Poné la cantidad devuelta.", true);
      if (q + already > line.qty + 1e-6) return toast("Se entregaron " + qty(line.qty) + ".", true);
      if (!(draft.retReason || "").trim()) return toast("Poné el motivo.", true);
      draft.returns.push({ uuid: uuid(), line_id: line.id, qty: q, reason: draft.retReason.trim() });
      draft.retLine = null; draft.retQty = ""; draft.retReason = ""; draft.adding = false;
      rerender();
    },
    // 19.0.1.129.22: «Entregar igual» sets (a re-opened visit already has it
    // on: a toggle turned it off with the usual tap); «deshacer» clears it
    "no-code": function (el, stop) {
      if (!returnsAnswered(draft)) return toast("Primero contestá ¿Devolvió algo?", true);
      if (draft.without_code) return;
      draft.without_code = true; draft.code = ""; draft.code_ok = false;
      rerender();
    },
    "no-code-undo": function () { draft.without_code = false; rerender(); },
    // 19.0.1.129.22: «Ya estaba pagado»: delivered, no cash, no transfer, no
    // «No pagó nada» (nothing at the driver's name)
    "already-paid": function (el, stop) { alreadyPaid(stop); },
    tr: function (el, stop) {
      draft.transfer = el.getAttribute("data-v");
      if (draft.transfer !== "yes") { draft.transfer_amount = 0; draft.receipt_answer = null; }
      else if (!draft.transfer_amount) {
        draft.transfer_amount = Math.max(round2(toCollect(stop, draft.returns) - (draft.cash || 0)), 0);
      }
      rerender();
    },
    rcpt: function (el) { draft.receipt_answer = el.getAttribute("data-v"); rerender(); },
    "red-ok": function () { draft.red_ok = !draft.red_ok; rerender(); },
    nd: function () { draft.outcome = "not_delivered"; rerender(); window.scrollTo(0, 0); },
    "undo-nd": function (el, stop) { draft.outcome = stop && stop.parcel ? "dispatched" : "delivered"; rerender(); },
    "parcel-save": function (el, stop) {
      var d = draft, pays = stop.parcel.payer === "distri";
      if (!d.photo && !d.thumb && !d.has_photo) return toast("Sacá la foto de la guía.", true);
      if (pays && isNaN(d.cost)) return toast("El costo no es un número.", true);
      if (pays && !(d.cost > 0)) return toast("Poné cuánto pagaste el envío.", true);
      if (!(parseInt(d.packages, 10) >= 1)) return toast("Poné cuántos bultos despachaste.", true);
      d.packages = parseInt(d.packages, 10);
      if (!pays) d.cost = 0;
      d.outcome = "dispatched";
      withGps("close_visit", "guardar la encomienda", function (gps) {
        storeParcel(stop, d, gps).then(function () { toast("Encomienda guardada en el celular."); });
        go("#visitas");
      });
    },
    "parcel-nd-save": function (el, stop) {
      var d = draft, lost = parcelDiscardedText(d);
      var save = function () {
        d.cost = 0; d.guide_number = "";
        withGps("close_visit", "guardar la visita", function (gps) {
          storeParcel(stop, d, gps).then(function () { toast("Guardado en el celular: no se despachó."); });
          go("#visitas");
        });
      };
      if (!lost) return save();
      askConfirm({ title: "No se despachó", danger: true, ok: "Sí, no se despachó",
                   text: "Se va a borrar lo que cargaste: " + lost + ". ¿Seguro que no se despachó?", onOk: save });
    },
    "exp-kind": function (el) { expDraft.kind = el.getAttribute("data-v"); rerender(); },
    "exp-save": function () {
      var d = expDraft;
      if (!d.kind) return toast("Elegí el tipo de gasto.", true);
      if (isNaN(d.amount) || !(d.amount > 0)) return toast("Poné el importe del gasto.", true);
      if (!d.photo) return toast("Sacá la foto del comprobante.", true);
      var e = { uuid: uuid(), kind: d.kind, amount: d.amount, comment: (d.comment || "").trim(), thumb: d.thumb,
                captured_at: new Date().toISOString(), synced: false, error: null };
      S.local.expenses[e.uuid] = e;
      var payload = { expense_uuid: e.uuid, kind: e.kind, amount: e.amount, comment: e.comment, photo: d.photo };
      saveLocal().then(function () { return enqueue("expense", payload, "expense:" + e.uuid); })
        .then(function () { toast("Gasto guardado en el celular."); });
      expDraft = null;
      render(); window.scrollTo(0, 0);
    },
    "exp-del": function (el) {
      var id = el.getAttribute("data-id"), e = S.local.expenses[id];
      if (!e || !window.confirm("¿Anular este gasto de " + money(e.amount) + "?")) return;
      e.deleted = true;
      var queued = S.outbox.filter(function (x) { return x.key === "expense:" + id; });
      var job = queued.length ?
        // never reached the server: drop it from the phone
        Promise.all(queued.map(function (x) { return outboxDel(x.uuid); })).then(function () {
          S.outbox = S.outbox.filter(function (x) { return queued.indexOf(x) < 0; });
          delete S.local.expenses[id];
          return saveLocal();
        }) :
        saveLocal().then(function () { return enqueue("expense", { expense_uuid: id, deleted: true }, "expense:" + id); });
      job.then(function () { toast("Gasto anulado."); rerender(); });
    },
    "save-nd": function (el, stop) {
      var d = draft, lost = discardedText(d);
      var save = function () {
        d.returns_answer = null; d.returns = []; d.code = ""; d.code_ok = false; d.without_code = false;
        d.cash = 0; d.transfer = "no"; d.transfer_amount = 0; d.receipt_answer = null; d.no_payment = false;
        d.already_paid = false; d.to_confirm = false;
        withGps("close_visit", "guardar la visita", function (gps) {
          storeVisit(stop, d, gps).then(function () { toast("Guardado en el celular: no se entregó."); });
          go("#visitas");
        });
      };
      if (!lost) return save();
      // «Volver: sí se entregó» keeps everything: only saving throws it away
      askConfirm({ title: "No se entregó", danger: true, ok: "Sí, no se entregó",
                   text: "Se va a borrar lo que cargaste: " + lost + ". ¿Seguro que no se entregó?", onOk: save });
    },
    "no-pay": function (el, stop) {
      var d = draft, err = validateDelivered(stop, d);
      if (err) return toast(err, true);
      if (d.cash > 0 || d.transfer !== "no") {
        return toast("Si pagó algo, guardá la visita. «No pagó nada» es sin efectivo ni transferencia.", true);
      }
      askConfirm({
        title: "¿No pagó nada?", ok: "Sí, no pagó nada",
        text: "Se guarda la visita de " + stop.name + " como entregada y sin cobro. El pedido de hoy (" +
          money(dueToday(stop, d.returns)) + ") queda pendiente a tu nombre: si no se cobra para la rendición " +
          "de mañana, se carga a tu cuenta.",
        onOk: function () {
          d.no_payment = true; d.already_paid = false; d.to_confirm = false;
          withGps("close_visit", "guardar la visita", function (gps) {
            storeVisit(stop, d, gps).then(function () { toast("Guardado: no pagó nada."); });
            go("#visitas");
          });
        },
      });
    },
    "confirm-yes": function () {
      var c = confirmState;
      closeConfirm();
      if (c && c.onOk) c.onOk();
    },
    "confirm-no": function () { closeConfirm(); },
    save: function (el, stop) {
      var err = validateDelivered(stop, draft);
      if (!err && isNaN(draft.cash)) err = "El efectivo no es un número.";
      if (!err && draft.transfer === "yes" && !(draft.transfer_amount > 0)) err = "Poné el importe transferido.";
      if (!err && draft.transfer === "yes" && !draft.receipt_answer) err = "Elegí cómo llega el comprobante.";
      if (!err && draft.transfer === "yes" && draft.receipt_answer === "photo" && !draft.photo && !draft.thumb &&
          !draft.receipt_photo) err = "Sacá la foto del comprobante.";
      // 19.0.1.129.22: nothing collected from an order the server says is
      // paid is «Ya estaba pagado», not «No pagó nada»
      if (!err && !(draft.cash > 0) && draft.transfer === "no" && stop.payments_paid) return alreadyPaid(stop);
      if (!err && !(draft.cash > 0) && draft.transfer === "no" && stop.payments_to_confirm) {
        err = "Si ya pagó con el comprobante cargado, tocá «" + TO_CONFIRM_LABEL + "». Si no pagó nada, «No pagó nada».";
      }
      if (!err && !(draft.cash > 0) && draft.transfer === "no") err = "Si no pagó nada, tocá «No pagó nada».";
      if (err) return toast(err, true);
      draft.no_payment = false; draft.already_paid = false; draft.to_confirm = false;
      var d = draft, cwa = whatsappOf(stop);
      withGps("close_visit", "guardar la visita", function (gps) {
        // «Al guardar la visita se abre WhatsApp con el pedido escrito»
        if (d.transfer === "yes" && d.receipt_answer === "not_yet" && cwa.number) {
          window.open(waLink(waDigits(cwa.number), visitClaim(stop, d.transfer_amount)), "_blank", "noopener");
        }
        storeVisit(stop, d, gps).then(function () { toast("Visita guardada en el celular."); });
        go("#visitas");
      });
    },
    "wa-ok": function (el, stop) {
      var wa = whatsappOf(stop);
      saveWhatsapp(stop, wa.number, "confirm");
    },
    "wa-edit": function () { draft.waEditing = true; draft.waNumber = null; rerender(); },
    "wa-cancel": function () { draft.waEditing = false; rerender(); },
    "wa-save": function (el, stop) {
      var number = (draft.waNumber != null ? draft.waNumber : whatsappOf(stop).number || "").trim();
      if (number.replace(/\D/g, "").length < 8) return toast("El número es muy corto.", true);
      draft.waEditing = false;
      saveWhatsapp(stop, number, whatsappOf(stop).number ? "correct" : "load");
    },
    "loc-ok": function () { custDraft.location = "confirm"; rerender(); },
    "loc-gps": function () {
      if (!navigator.geolocation) return toast("El celular no da la ubicación.", true);
      toast("Buscando la ubicación…");
      var d = custDraft;
      navigator.geolocation.getCurrentPosition(function (pos) {
        if (custDraft !== d) return;
        d.lat = pos.coords.latitude; d.lng = pos.coords.longitude; d.location = "gps"; d.distance = 0;
        toast("Ubicación tomada (±" + Math.round(pos.coords.accuracy) + " m). Tocá Guardar.");
        rerender();
      }, function (e) {
        toast(e.code === 1 ? "Permití la ubicación para esta página." : "No se pudo tomar la ubicación.", true);
      }, { enableHighAccuracy: true, timeout: 20000, maximumAge: 0 });
    },
    day: function (el) {
      var v = +el.getAttribute("data-v"), i = custDraft.days.indexOf(v);
      if (i >= 0) custDraft.days.splice(i, 1); else custDraft.days.push(v);
      custDraft.days.sort();
      rerender();
    },
    "h-add": function () { custDraft.hours.push(["", ""]); rerender(); },
    "h-del": function (el) { custDraft.hours.splice(+el.getAttribute("data-i"), 1); rerender(); },
    "cust-save": function (el, stop) {
      var d = custDraft;
      var hours = d.hours.filter(function (r) { return r[0] && r[1]; });
      if (hours.some(function (r) { return r[1] <= r[0]; })) return toast("Revisá los horarios: el fin va después del inicio.", true);
      var payload = {
        partner_id: stop.shipping_partner_id, days: d.days.join(","),
        hours: hours.map(function (r) { return r[0] + "-" + r[1]; }).join(";"), note: d.note,
      };
      if (d.location) { payload.location = d.location; payload.lat = d.lat; payload.lng = d.lng; }
      var prev = customerOf(stop);
      S.local.customers[stop.shipping_partner_id] = {
        lat: d.lat, lng: d.lng, days: payload.days, hours: payload.hours, note: d.note,
        location_by: d.location ? S.user : prev.location_by,
        location_date: d.location ? new Date().toISOString() : prev.location_date,
      };
      saveLocal().then(function () {
        return enqueue("customer", payload, null);
      }).then(function () { toast("Datos del cliente guardados en el celular."); });
      go("#visita/" + stop.id);
    },
    close: function () { closeRoute(false); },
    // 19.0.1.129.25: no reason asked (owner's decision); the server logs who and when
    "reopen-route": function () {
      if (!canReopen()) return;
      if (navigator.onLine === false) return toast("Sin señal: para reabrir la ruta hace falta señal.", true);
      askConfirm({
        title: "¿Reabrir la ruta?", ok: "Sí, reabrir",
        text: "La ruta vuelve a «En reparto» para que corrijas lo que haga falta. Después cerrala de nuevo. " +
          "Queda registrado que la reabriste vos.",
        onOk: function () { reopenRoute(); },
      });
    },
    arrive: function (el, stop) {
      if (!stop) return;
      withGps("arrive", "marcar «Llegué»", function (gps) {
        S.local.arrivals[stop.id] = { at: new Date().toISOString() };
        var payload = { stop_id: stop.id };
        if (gps !== undefined) payload.gps = gps;
        saveLocal().then(function () { return enqueue("arrive", payload, "arrive:" + stop.id); });
        render(); window.scrollTo(0, 0);
      });
    },
    "gps-retry": function () {
      var g = gpsState;
      if (!g) return;
      gpsState = { busy: true, label: g.label }; paintGps();
      // with signal, the route comes again: administración may have
      // authorized it «Sin GPS» in the meantime
      var refresh = navigator.onLine ? download(false) : Promise.resolve();
      refresh.then(function () { gpsState = null; withGps(g.kind, g.label, g.fn); });
    },
    "gps-cancel": function () { gpsState = null; paintGps(); },
    "gps-accept": function () {
      var g = gpsState, conf = gpsConf();
      if (!g || !conf) return;
      S.gpsNotice = conf.notice_version;
      kvSet(S.uid === null ? "gpsNotice" : userKey("gpsNotice"), conf.notice_version);
      enqueue("gps_notice", { version: conf.notice_version }, "gps_notice");
      gpsState = null;
      withGps(g.kind, g.label, g.fn);
    },
    "next-route": function () {
      // 19.0.1.113.0: what this route still has to send stays under it
      explicitSwitch = true;
      download(true);
    },
    "open-route": function (el) {
      var id = +el.getAttribute("data-id");
      if (S.route && S.route.id === id) return go("#");
      // the copy on the phone first (it may have no signal), then the server's
      kvGet(userKey("route", id)).then(function (route) {
        if (route) {
          saveLocal();
          S.route = route;
          S.local = fillLocal(S.locals[id] || emptyLocal(id));
          S.locals[id] = S.local;
          saveRoute();
        }
        wanted = id; explicitSwitch = true;
        go("#");
        return download(true);
      });
    },
    "gone-ok": function () {
      var id = S.gone && S.gone.id;
      S.gone = null;
      kvDel(userKey("gone"));
      if (id && !pendingFor(id) && !(S.route && S.route.id === id)) forgetRoute(id);
      rerender();
    },
  };

  function alreadyPaid(stop) {
    var d = draft, err = validateDelivered(stop, d);
    if (err) return toast(err, true);
    var lost = [];
    if (d.cash > 0) lost.push("efectivo " + money(d.cash));
    if (d.transfer === "yes") lost.push("transferencia de " + money(d.transfer_amount || 0));
    // 19.0.1.129.25: paid with a receipt still to credit: «a confirmar»
    var toConfirm = !stop.payments_paid && !!stop.payments_to_confirm;
    askConfirm({
      title: toConfirm ? "¿Ya estaba pagado · a confirmar?" : "¿Ya estaba pagado?",
      ok: toConfirm ? "Sí, a confirmar" : "Sí, ya estaba pagado",
      text: "Se guarda la visita de " + stop.name + " como entregada, sin cobro: " +
        (toConfirm ? "el cliente cargó un comprobante de " + money(stop.payments_to_confirm_amount || 0) +
          " que cubre lo que debe. Queda «a confirmar»: administración lo verifica con Mercado Pago." :
          "el pedido ya figura pagado" + (stop.payments_paid_how ? " (" + stop.payments_paid_how + ")" : "") + ".") +
        " No queda nada a tu nombre." +
        (lost.length ? " Se borra lo que cargaste: " + lost.join(", ") + "." : ""),
      onOk: function () {
        d.cash = 0; d.transfer = "no"; d.transfer_amount = 0; d.receipt_answer = null;
        d.no_payment = false; d.already_paid = true; d.to_confirm = toConfirm;
        withGps("close_visit", "guardar la visita", function (gps) {
          storeVisit(stop, d, gps).then(function () { toast("Guardado: ya estaba pagado."); });
          go("#visitas");
        });
      },
    });
  }

  function saveWhatsapp(stop, number, action) {
    S.local.whatsapp[stop.partner_id] = { number: number, confirmed: true };
    saveLocal().then(function () {
      return enqueue("whatsapp", { partner_id: stop.partner_id, number: number, action: action }, "wa:" + stop.partner_id);
    }).then(function () { toast("WhatsApp confirmado."); });
    rerender();
  }

  function closeRoute(refreshed) {
    var r = closeSummary();
    if (r.sum.pending) return toast("Quedan " + r.sum.pending + " visitas sin estado.", true);
    var list = stops().filter(function (s) { return stopState(s) === "not_delivered"; });
    var missing = list.filter(function (s) { return !(visitOf(s).reason || "").trim(); });
    if (missing.length) return toast("Falta el motivo de lo no entregado.", true);
    if (r.noGuide.length) return toast("Falta la foto de la guía de " + r.noGuide[0].name + ".", true);
    if (r.unsynced && !refreshed && navigator.onLine !== false) {
      // 19.0.1.129.22: administración may have resolved them: ask the server first
      toast("Consultando al servidor…");
      return download(false).then(function () { closeRoute(true); });
    }
    if (r.unsynced) return toast("Hay visitas o gastos que el servidor rechazó. Abrilos y guardalos de nuevo.", true);
    withGps("end", "cerrar la ruta", sendClose);
  }

  function sendClose(gps) {
    toast("Enviando…");
    syncNow().then(function () {
      if (S.outbox.length) {
        throw new Error(S.auth ? "Sin señal: quedan cambios sin enviar. Buscá señal y probá de nuevo." :
          "Tu sesión venció. Entrá de nuevo para cerrar la ruta.");
      }
      return enqueue("close", gps !== undefined ? { gps: gps } : {}, "close");
    }).then(function () { return syncNow(); }).then(function () {
      if (S.outbox.length) throw new Error("Sin señal: la ruta se cierra cuando se envíe todo.");
      // a rejected close is redone from this screen (the server closes it
      // when a later close goes through)
      var rej = S.local.rejected.filter(function (x) { return x.type === "close"; });
      if (rej.length) {
        S.local.rejected = S.local.rejected.filter(function (x) { return x.type !== "close"; });
        throw new Error(rej[rej.length - 1].error);
      }
      S.local.closed = true;
      return saveLocal().then(function () { toast("Ruta cerrada."); go("#"); });
    }).catch(function (e) { toast(e.message, true); render(); });
  }

  document.addEventListener("input", function (ev) {
    var el = ev.target, f = el.getAttribute && el.getAttribute("data-f");
    if (!f) return;
    var parts = location.hash.replace(/^#/, "").split("/");
    var stop = stopById(parts[1]);
    if (f === "code") {
      draft.code = el.value.replace(/\D/g, "").slice(0, 4);
      if (el.value !== draft.code) el.value = draft.code;
      draft.code_ok = false;
      var st = document.getElementById("code-status");
      if (st) st.innerHTML = codeStatus(stop);
      checkCode(stop);
    } else if (f === "cash") {
      draft.cash = parseMoney(el.value);
    } else if (f === "tamount") {
      draft.transfer_amount = parseMoney(el.value);
    } else if (f === "pcost") draft.cost = parseMoney(el.value);
    else if (f === "guide") draft.guide_number = el.value;
    else if (f === "packages") draft.packages = el.value.replace(/\D/g, "");
    else if (f === "exp-amount") expDraft.amount = parseMoney(el.value);
    else if (f === "exp-comment") expDraft.comment = el.value;
    else if (f === "comment") draft.comment = el.value;
    else if (f === "reason") draft.reason = el.value;
    else if (f === "ret-qty") draft.retQty = el.value;
    else if (f === "ret-reason") draft.retReason = el.value;
    else if (f === "wa") draft.waNumber = el.value;
    else if (f === "note") custDraft.note = el.value;
    else if (f === "h-from" || f === "h-to") custDraft.hours[+el.getAttribute("data-i")][f === "h-from" ? 0 : 1] = el.value;
  });

  // 19.0.1.115.0: an amount is edited without «$» and thousands dots (the
  // formatted «$ 14.307,6» comes back on change)
  document.addEventListener("focusin", function (ev) {
    var el = ev.target, f = el && el.getAttribute && el.getAttribute("data-f");
    var value = f === "cash" && draft ? draft.cash
      : f === "tamount" && draft ? draft.transfer_amount
      : f === "pcost" && draft ? draft.cost
      : f === "exp-amount" && expDraft ? expDraft.amount : undefined;
    if (value === undefined || isNaN(value)) return;
    el.value = moneyRaw(value);
  });

  document.addEventListener("change", function (ev) {
    var el = ev.target, f = el.getAttribute && el.getAttribute("data-f");
    if (f === "ret-line") draft.retLine = +el.value || null;
    if (f === "cash" && draft) {
      el.value = draft.cash ? money(draft.cash) : "";
      // the «exigí el total» warning follows the cash
      if (draft.transfer === "yes") refreshTransferBits(stopById(location.hash.replace(/^#/, "").split("/")[1]));
    }
    if (f === "tamount" && draft) {
      el.value = draft.transfer_amount ? money(draft.transfer_amount) : "";
      refreshTransferBits(stopById(location.hash.replace(/^#/, "").split("/")[1]));
    }
    if (f === "pcost" && draft) el.value = draft.cost ? money(draft.cost) : "";
    if (f === "exp-amount" && expDraft) el.value = expDraft.amount ? money(expDraft.amount) : "";
    if (f === "photo" && el.files && el.files[0]) {
      var target = location.hash.indexOf("#gasto") === 0 ? expDraft : draft;
      if (!target) return;
      toast("Preparando la foto…");
      readPhoto(el.files[0]).then(function (res) {
        target.photo = res.photo; target.thumb = res.thumb;
        toast("Foto lista (" + Math.round(res.photo.length * 0.75 / 1024) + " KB).");
        rerender();
      }).catch(function (e) { toast(e.message || "No se pudo leer la foto.", true); });
    }
    if (f === "rphoto" && el.files && el.files[0] && draft) {
      var rd = draft;
      toast("Preparando el comprobante…");
      readReceipt(el.files[0]).then(function (res) {
        if (draft !== rd) return;
        // a new photo is a new receipt for the server: a new id
        rd.photo = res.photo; rd.thumb = res.thumb; rd.photo_uuid = uuid(); rd.receipt_answer = "photo";
        toast((res.pdf ? "PDF listo (" : "Foto lista (") + Math.round(res.photo.length * 0.75 / 1024) + " KB). Guardá la visita.");
        rerender();
      }).catch(function (e) { toast(e.message || "No se pudo leer la foto.", true); });
    }
    if (f === "nd-reason") {
      var stop = stopById(el.getAttribute("data-id"));
      var v = visitOf(stop), reason = el.value.trim();
      if (v && reason !== (v.reason || "")) {
        var d = stop.parcel ? newParcelDraft(stop) : JSON.parse(JSON.stringify(v));
        d.reason = reason;
        if (stop.parcel) storeParcel(stop, d); else storeVisit(stop, d);
      }
    }
  });

  // --------------------------------------------------------------- start

  window.addEventListener("online", function () { S.online = true; paintStatus(); syncNow(); });
  window.addEventListener("offline", function () { S.online = false; paintStatus(); });
  setInterval(function () { if (S.outbox.length) syncNow(); }, SYNC_EVERY_MS);
  setInterval(pollLoad, LOAD_POLL_MS);
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") syncNow();
  });

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/reparto/sw.js", { scope: "/reparto" }).catch(function () {});
  }
  if (navigator.storage && navigator.storage.persist) {
    navigator.storage.persist().catch(function () {});
  }

  // 19.0.1.129.16: what the extension loaded after this file (app4.js) reads
  window.RepartoApp = {
    route: function () { return S.route; },
    stopById: stopById,
    online: function () { return S.online; },
    departed: function () { return !!(S.local && S.local.departed); },
    saveRoute: saveRoute,
    rerender: rerender,
    money: money,
    h: h,
    fetchWithTimeout: fetchWithTimeout,
    isLoginPage: isLoginPage,
    api: { load: API_LOAD },
    // 19.0.1.129.22: the rejections of the poll (app4.js)
    onRejections: onRejections,
    // 19.0.1.129.25: the route's state of the poll (app4.js)
    onRouteState: onRouteState,
    // 19.0.1.129.28: what app5.js («Reparto v2») uses
    local: function () { return S.local; },
    uid: function () { return S.uid; },
    user: function () { return S.user; },
    otherRoutes: function () { return S.otherRoutes || []; },
    getDraft: function () { return draft; },
    setDraft: function (d) { draft = d; },
    newDraft: newDraft,
    storeVisit: storeVisit,
    withGps: withGps,
    go: go,
    askConfirm: askConfirm,
    toast: toast,
    stops: stops,
    visitOf: visitOf,
    stopState: stopState,
    statusBanner: statusBanner,
    noticeHtml: noticeHtml,
    packedHtml: packedHtml,
    accountLink: accountLink,
    needsArrival: needsArrival,
    redOf: redOf,
    returnsAmount: returnsAmount,
    returnsAnswered: returnsAnswered,
    codeStatus: codeStatus,
    customerOf: customerOf,
    daysHoursText: daysHoursText,
    whatsappOf: whatsappOf,
    // 19.0.1.129.33: the customer's WhatsApp from «Ubicación y horarios»
    // (the same «whatsapp» event as the old visit) and «Reabrir la ruta»
    saveWhatsapp: saveWhatsapp,
    canReopen: canReopen,
    waLink: waLink,
    waDigits: waDigits,
    visitClaim: visitClaim,
    kindLabel: kindLabel2,
    relWhen: relWhen,
    saveLocal: saveLocal,
    kvGet: kvGet,
    kvSet: kvSet,
    userKey: userKey,
    enqueueRoute: enqueueRoute,
    readReceipt: readReceipt,
    uuid: uuid,
    round2: round2,
    qty: qty,
    reasons: REASONS,
    icons: { camera: ICON_CAMERA, check: ICON_CHECK, pin: ICON_PIN },
    actions: ACTIONS,
  };

  boot();

  // 19.0.1.113.0: the user of the page (the shell sets data-uid; without
  // signal it is the cached shell of the last user online) picks whose state
  // opens, so another user's route is never shown, not even for a moment.
  // Without a known user (a phone coming from the older app) the state of
  // the older app opens and nothing is sent until the server names the user.
  function boot() {
    var shellUid = +(document.body && document.body.getAttribute && document.body.getAttribute("data-uid")) || null;
    return kvGet("uid").then(function (stored) {
      var uid = shellUid || stored || null;
      if (uid) return loadUser(uid);
      return Promise.all([kvGet("route"), kvGet("local"), outboxAll(), kvGet("gpsNotice")]).then(function (res) {
        S.gpsNotice = res[3] || null;
        S.route = res[0] || null;
        S.local = fillLocal(res[1] || emptyLocal(S.route ? S.route.id : null));
        if (S.local.routeId) S.locals[S.local.routeId] = S.local;
        S.outbox = (res[2] || []).filter(function (e) { return !e.uid; }).sort(byCapture);
      });
    }).then(function () {
      render();
      return syncNow().then(function () { return download(false); }).then(function () {
        // the older app's events wait for the first download
        if (S.outbox.length) return syncNow();
      });
    }).catch(function (err) {
      app.innerHTML = '<div class="cargando">Este celular no deja guardar datos (' + h(err && err.message) +
        "). Probá con Chrome y sin modo incógnito.</div>";
    });
  }
})();
