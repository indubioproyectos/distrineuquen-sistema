// Runs the driver app (static/src/reparto/app3.js, app4.js and app5.js) in a
// Node vm and prints the 19.0.1.129.28 «Reparto v2» checks as JSON:
// - flag off: with app5.js loaded every screen is the same HTML as without
//   it (app3.js + app4.js), the old visit saves the old payload, and
//   /reparto/api/home {"v2": false} adds nothing;
// - the old app (app3.js + app4.js, no app5.js) against a flag-on server
//   (extra keys in the route) draws and sends what it did before;
// - flag on: the new visit (four taps for a delivery in cash), transfer «lo
//   manda después», partial (charged / exempt), mixed, «Ya estaba pagado»
//   with debt, the optional code, «Mis comprobantes a reclamar», the
//   reminder and «Cambiar el orden»;
// - an outbox left by the older app survives the update and is sent.
// Optional third argument: an older app3.js (e.g. 19.0.1.129.27's) whose
// screens must equal the flag-off ones.
// Used by tests/test_v129_28_reparto_v2.py.
//   node reparto_v129_28.js <module root> [old app3.js]
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = process.argv[2];
const oldApp3 = process.argv[3] || "";
const read = (p) => fs.readFileSync(path.join(root, p), "utf8");
const app4 = read("static/src/reparto/app4.js");
const app5 = read("static/src/reparto/app5.js");

function instrument(src) {
  const MARK = "  // --------------------------------------------------------------- start";
  if (!src.includes(MARK)) throw new Error("start mark not found");
  const BOOT = "\n  boot();\n";
  if (!src.includes(BOOT)) throw new Error("boot call not found");
  src = src.replace(MARK, [
    "  var ST = globalThis.__store, cp = function (v) { return v === undefined ? undefined : JSON.parse(JSON.stringify(v)); };",
    "  kvGet = function (k) { return Promise.resolve(cp(ST.kv.get(k))); };",
    "  kvSet = function (k, v) { ST.kv.set(k, cp(v)); return Promise.resolve(); };",
    "  kvDel = function (k) { ST.kv.delete(k); return Promise.resolve(); };",
    "  kvKeys = function () { return Promise.resolve(Array.from(ST.kv.keys())); };",
    "  outboxAll = function () { return Promise.resolve(Array.from(ST.outbox.values()).map(cp)); };",
    "  outboxPut = function (ev) { ST.outbox.set(ev.uuid, cp(ev)); return Promise.resolve(); };",
    "  outboxDel = function (id) { ST.outbox.delete(id); return Promise.resolve(); };",
  ].join("\n") + "\n" + MARK);
  return src.replace(BOOT, [
    "",
    "  globalThis.__T = { S: S, boot: boot, download: download, syncNow: syncNow, ACTIONS: ACTIONS,",
    "    render: render, stopById: stopById, viewVisit: viewVisit, viewVisits: viewVisits,",
    "    viewEmpty: viewEmpty, viewClosed: viewClosed, viewClose: viewClose, money: money,",
    "    draft: function () { return draft; }, confirm: function () { return confirmState; } };",
    "",
  ].join("\n"));
}
const app3 = instrument(read("static/src/reparto/app3.js"));
const app3old = oldApp3 ? instrument(fs.readFileSync(oldApp3, "utf8")) : "";

const elements = {};
function element(id) {
  if (!elements[id]) elements[id] = { id: id, innerHTML: "", textContent: "", hidden: true, className: "", value: "" };
  return elements[id];
}
const listeners = {};
const body = { attrs: {}, getAttribute(name) { return this.attrs[name] || null; }, appendChild() {} };
const server = { calls: [], route: null, home: null, order: null };
function respond(status, data) {
  return Promise.resolve({
    ok: status < 400, status: status, redirected: false,
    headers: { get: () => "application/json" },
    json: () => Promise.resolve(JSON.parse(JSON.stringify(data))),
  });
}
function fakeFetch(url, options) {
  const call = { url: url, body: options && options.body ? JSON.parse(options.body) : null };
  server.calls.push(call);
  if (url.indexOf("/reparto/api/route") === 0) return respond(200, server.route(url));
  if (url.indexOf("/reparto/api/home") === 0) {
    if (!server.home) return respond(404, { ok: false });
    return respond(200, server.home());
  }
  if (url.indexOf("/reparto/api/order") === 0) {
    const out = server.order ? server.order(call.body) : { status: 404, data: { ok: false } };
    return respond(out.status || 200, out.data);
  }
  if (url.indexOf("/reparto/api/sync") === 0) {
    return respond(200, { results: call.body.events.map((e) => ({ uuid: e.uuid, status: "ok" })) });
  }
  return Promise.reject(new TypeError("offline"));
}
const noop = () => {};
const ctx = {
  console, Intl, Promise, Math, JSON, Date, encodeURIComponent, Array, Object, Number, String, URL, isNaN,
  navigator: { onLine: true },
  crypto: require("crypto").webcrypto,
  window: { addEventListener: noop, open: noop, scrollTo: noop },
  document: {
    addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
    getElementById: element, createElement: () => element("new"), body: body,
    querySelector: () => null, visibilityState: "hidden", activeElement: null,
  },
  setInterval: noop, setTimeout: noop, clearTimeout: noop, location: { hash: "" },
  indexedDB: {}, fetch: fakeFetch,
};
ctx.globalThis = ctx;
ctx.self = ctx;
vm.createContext(ctx);
vm.runInContext(read("static/src/reparto/claim.js"), ctx);
ctx.window.DistriClaim = ctx.DistriClaim;

function freshApp(opts) {
  opts = opts || {};
  ctx.__store = { kv: new Map(Object.entries(opts.kv || { uid: 7 })),
                  outbox: new Map((opts.outbox || []).map((e) => [e.uuid, e])) };
  body.attrs = { "data-uid": "7" };
  server.calls = [];
  ctx.location.hash = "";
  ctx.window.RepartoExt = undefined;
  ctx.window.RepartoApp = undefined;
  ctx.window.RepartoV2 = undefined;
  for (const k of Object.keys(listeners)) delete listeners[k];
  vm.runInContext(opts.old ? app3old : app3, ctx);
  const T = ctx.__T;
  T.withApp4 = !opts.noApp4;
  if (!opts.noApp4) vm.runInContext(app4, ctx);
  if (opts.app5) vm.runInContext(app5, ctx);
  return T;
}
const flush = async () => { for (let i = 0; i < 40; i++) await new Promise((r) => setImmediate(r)); };
function fire(type, target) {
  (listeners[type] || []).forEach((fn) => fn({ type: type, target: target, preventDefault: noop }));
}
function el(attrs, value) {
  const e = { value: value || "", tagName: "BUTTON", disabled: false,
    getAttribute(n) { return attrs[n] === undefined ? null : String(attrs[n]); } };
  e.closest = (sel) => {
    const m = /^\[([\w-]+)\]$/.exec(sel);
    return m && attrs[m[1]] !== undefined ? e : null;
  };
  return e;
}
const tap5 = (a, v, extra) => fire("click", el(Object.assign({ "data-a5": a, "data-v": v || "" }, extra || {})));
const tap3 = (a, v) => fire("click", el({ "data-a": a, "data-v": v || "" }));
const type = (f, value) => fire("input", el({ "data-f": f }, value));
const screen = (T, hash) => { ctx.location.hash = hash; T.render(); return element("app").innerHTML; };
const lastVisit = () => {
  const sent = server.calls.filter((c) => c.url.indexOf("/sync") >= 0)
    .map((c) => c.body.events).reduce((a, b) => a.concat(b), []).filter((e) => e.type === "visit");
  return sent.length ? sent[sent.length - 1].payload : null;
};

const stop = (id, extra) => Object.assign({ id: id, seq: 1, order_id: id, order: "S" + id, document: "Factura " + id,
  state: "pending", delivery_state: "pending", loaded: true, partner_id: 9, commercial_partner_id: 9,
  shipping_partner_id: 9, name: "KIOSCO " + id, town: "Centenario", address: "Calle " + id, parcels: 2, zone: "",
  parcel: null, lines: [{ id: 1, product: "Producto", qty: 10, uom: "u", unit_price: 100 }],
  order_amount: 1000, previous_debt: 0, paid_elsewhere: 0, retained: "", code_locked: false,
  code_tries_left: 5, code_attempts_recorded: 0, code_salt: "s", code_hash: "h", whatsapp: "299 5000111",
  whatsapp_confirmed: false, account: true, customer: {}, notice: null, comment: "", depot_note: "",
  red: null, visit: null, payments: [], payments_due: 1000, payments_paid: false }, extra || {});
function routeOf(v2) {
  const s = [stop(801), stop(802, { v2_exempt: true }), stop(803, { payments_paid: true, payments_due: 0,
    payments_paid_how: "Mercado Pago · 20/11" }), stop(804), stop(805)];
  if (v2) s.forEach((x) => { x.v2_due = x.payments_paid ? 0 : 1000; if (x.v2_exempt === undefined) x.v2_exempt = false; });
  const r = { id: 60, name: "Ruta 60", date: "2026-11-25", trip: "1", state: "on_route", zones: "Centro",
    stops: s, left: [], expenses: [], receipts: [], rejections: [] };
  if (v2) r.reparto_v2 = true;
  return r;
}
const pick = { id: 60, name: "Ruta 60", state: "on_route", state_label: "En reparto", title: "Viaje 1", detail: "25/11" };
const pickOld = { id: 55, name: "Ruta 55", state: "on_route", state_label: "En reparto", title: "Viaje 1", detail: "20/11" };
const homeV2 = () => ({ ok: true, v2: true, user_id: 7,
  receipts: [{ stop_id: 501, route_id: 55, partner_id: 31, document: "Factura 501", order: "S501", name: "ALMACÉN SUR",
               amount: 25000, state: "open", kind: "done", delivered_at: "2026-11-20 15:00:00", overdue_since: "",
               resolved_by: "", whatsapp: "5492995000222", claim: "Hola, te entregué…", line: "" },
             { stop_id: 502, route_id: 55, partner_id: 32, document: "Factura 502", order: "S502", name: "KIOSCO NORTE",
               amount: 5000, state: "fulfilled", kind: "done", delivered_at: "2026-11-25 10:00:00", overdue_since: "",
               resolved_by: "Cliente", whatsapp: "", claim: "", line: "" }],
  charges: [{ id: 9, kind: "short", label: "Transferencia menor al total", amount: 400, name: "KIOSCO OESTE",
              order: "S503", date: "24/11", text: "pagó $ 600" }],
  stale_routes: [{ id: 55, name: "Ruta 55", date: "20/11" }] });

(async () => {
  const out = {};
  const V2 = { on: false };
  server.route = () => ({ ok: true, user: "Simón", user_id: 7, routes: V2.on ? [pick, pickOld] : [pick],
                          route: routeOf(V2.on) });

  // ---- 1. flag off: app5.js loaded changes no screen
  V2.on = false;
  server.home = () => ({ ok: true, v2: false });
  let base = freshApp({});
  base.boot(); await flush();
  const without = { visits: screen(base, "#visitas"), visit: screen(base, "#visita/801"),
                    paid: screen(base, "#visita/803"), account: base.viewEmpty() };
  let T = freshApp({ app5: true });
  T.boot(); await flush();
  const withApp5 = { visits: screen(T, "#visitas"), visit: screen(T, "#visita/801"),
                     paid: screen(T, "#visita/803"), account: T.viewEmpty() };
  out.flagOffSame = JSON.stringify(without) === JSON.stringify(withApp5);
  out.flagOffOldVisit = withApp5.visit.indexOf("¿Devolvió algo?") >= 0 && withApp5.visit.indexOf("Le toca pagar") < 0;
  out.flagOffNoOrderLink = withApp5.visits.indexOf("Cambiar el orden") < 0;
  out.flagOffNoHome = withApp5.visits.indexOf("Mis comprobantes a reclamar") < 0;
  out.flagOffHomeAsked = server.calls.some((c) => c.url.indexOf("/reparto/api/home") === 0);
  // the old visit, saved as before
  screen(T, "#visita/801");
  T.ACTIONS["ret-none"]();
  T.ACTIONS["no-code"](null, T.stopById(801));
  T.draft().cash = 1000;
  T.ACTIONS.save(null, T.stopById(801));
  await flush();
  const p0 = lastVisit() || {};
  out.flagOffPayload = { cash: p0.cash, without_code: p0.without_code, returns_answer: p0.returns_answer,
                         transfer: p0.transfer, keys: Object.keys(p0).sort().join(",") };
  // and against a server older than 129.28 (/reparto/api/home is a 404)
  server.home = null;
  T = freshApp({ app5: true });
  T.boot(); await flush();
  out.oldServerSame = screen(T, "#visita/801") === without.visit;
  if (app3old) {
    // the 129.27 app3.js draws the same screens as the flag-off ones
    const O = freshApp({ old: true });
    O.boot(); await flush();
    out.same12927 = screen(O, "#visitas") === without.visits && screen(O, "#visita/801") === without.visit &&
      screen(O, "#visita/803") === without.paid;
  }

  // ---- 2. the old app (no app5.js) against a flag-on server
  V2.on = true;
  server.home = homeV2;
  const O2 = freshApp({});
  O2.boot(); await flush();
  const oldVisit = screen(O2, "#visita/801");
  out.oldAppOldVisit = oldVisit.indexOf("¿Devolvió algo?") >= 0 && oldVisit.indexOf("Le toca pagar") < 0;
  O2.ACTIONS["ret-none"]();
  O2.ACTIONS["no-code"](null, O2.stopById(801));
  O2.draft().cash = 600;
  O2.ACTIONS.save(null, O2.stopById(801));
  await flush();
  const p1 = lastVisit() || {};
  out.oldAppPayload = { cash: p1.cash, without_code: p1.without_code, keys: Object.keys(p1).sort().join(",") };

  // ---- 3. flag on: the new visit
  T = freshApp({ app5: true });
  T.boot(); await flush();
  const visit = screen(T, "#visita/801");
  out.v2Visit = { due: visit.indexOf("Le toca pagar") >= 0 && visit.indexOf("$ 1.000") >= 0,
                  noReturnsQuestion: visit.indexOf("¿Devolvió algo?") < 0,
                  codeOptional: visit.indexOf("Código de recibido (opcional)") >= 0,
                  noWhatsappCard: visit.indexOf("WhatsApp del cliente, según el contacto") < 0,
                  buttons: ["Entregué todo", "Devolvió algo", "No se entregó", "Efectivo", "Transferencia", "Mixto",
                            "Parcial", "Ya estaba pagado", "No pagó"].every((b) => visit.indexOf(b) >= 0) };
  // four taps: Entregué todo, Efectivo, Guardar visita, Sí, guardar
  let taps = 0;
  tap5("deliv", "all"); taps++;
  tap5("pay", "cash"); taps++;
  tap5("save"); taps++;
  out.cashConfirm = (T.confirm() || {}).text || "";
  tap3("confirm-yes"); taps++;
  await flush();
  const p2 = lastVisit() || {};
  out.cash = { taps: taps, cash: p2.cash, transfer: p2.transfer, without_code: p2.without_code,
               returns_answer: p2.returns_answer, already_paid: p2.already_paid, no_payment: p2.no_payment,
               state: T.S.local.visits[801] && T.S.local.visits[801].cash };
  // transfer, the customer sends the receipt later
  screen(T, "#visita/804");
  tap5("deliv", "all");
  tap5("pay", "transfer");
  let v = screen(T, "#visita/804");
  // 26/11/2026 (drivers): the products to check are on the v2 visit too
  out.visitLines = v.indexOf("Controlá con el cliente") >= 0 && v.indexOf("Producto") >= 0;
  out.transferChoices = v.indexOf("Sacar foto") >= 0 && v.indexOf("Lo manda después · queda a mi cargo") >= 0;
  tap5("save");
  out.transferNeedsReceipt = element("toast").textContent;
  tap3("rcpt", "not_yet");
  tap5("save");
  tap3("confirm-yes");
  await flush();
  const p3 = lastVisit() || {};
  out.transfer = { cash: p3.cash, transfer: p3.transfer, amount: p3.transfer_amount, receipt: p3.receipt_answer };
  // partial: charged to the driver
  screen(T, "#visita/805");
  tap5("deliv", "all");
  tap5("pay", "partial");
  T.draft().cash = 600;
  type("cash", "600");
  v = screen(T, "#visita/805");
  out.partialWarning = v.indexOf("Faltan $ 400: quedan a tu cargo") >= 0;
  tap5("save");
  out.partialConfirm = (T.confirm() || {}).text || "";
  tap3("confirm-yes");
  await flush();
  out.partial = { cash: (lastVisit() || {}).cash, transfer: (lastVisit() || {}).transfer };
  // partial of an exempt customer (decision C)
  screen(T, "#visita/802");
  tap5("deliv", "all");
  tap5("pay", "partial");
  T.draft().cash = 300;
  v = screen(T, "#visita/802");
  out.exemptWarning = v.indexOf("quedan a cuenta del cliente") >= 0;
  // mixed: the transfer completes the rest
  tap5("pay", "mixed");
  T.draft().cash = 300;
  type("cash", "300");
  out.mixed = T.draft().transfer_amount;
  // «Ya estaba pagado» with debt: accepted, administración reviews it
  tap5("pay", "already");
  v = screen(T, "#visita/802");
  out.alreadyWithDebt = v.indexOf("administración lo revisa") >= 0;
  tap5("save");
  out.alreadyConfirm = (T.confirm() || {}).text || "";
  tap3("confirm-yes");
  await flush();
  const p4 = lastVisit() || {};
  out.already = { already_paid: p4.already_paid, cash: p4.cash, transfer: p4.transfer, no_payment: p4.no_payment };
  // an order the server says is paid: «Ya estaba pagado» chosen already
  v = screen(T, "#visita/803");
  out.paidPreselected = v.indexOf("Ya está pagado") >= 0 && /class="opt teal on" data-a5="pay" data-v="already"/.test(v);
  // a right code is kept, a missing one never blocks
  screen(T, "#visita/801");
  out.reopenedMode = T.draft().pay;

  // ---- 4. «Mis comprobantes a reclamar», the reminder, the order
  await T.download(false); await flush();
  await ctx.window.RepartoV2.fetchHome(true); await flush();
  const visits = screen(T, "#visitas");
  // 19.0.1.129.48: the block lists the receipt itself, its amount and its
  // actions; no total of receipts and charges ($ 25.400) any more
  out.homeCard = visits.indexOf("Mis comprobantes a reclamar") >= 0 && visits.indexOf("ALMACÉN SUR") >= 0 &&
    visits.indexOf("$ 25.000") >= 0 && visits.indexOf("$ 25.400") < 0 && visits.indexOf("A tu cargo") < 0;
  out.reminder = visits.indexOf("Tenés la ruta del 20/11 sin cerrar") >= 0 && visits.indexOf('data-a="open-route" data-id="55"') >= 0;
  out.orderLink = visits.indexOf('href="#orden"') >= 0;
  const claims = screen(T, "#reclamar");
  out.claims = { photo: claims.indexOf("Sacar foto") >= 0, whatsapp: claims.indexOf("Pedir por WhatsApp") >= 0,
                 charge: claims.indexOf("Transferencia menor al total") >= 0, resolved: claims.indexOf("Resueltas hoy") >= 0,
                 // 29912d8 (owner): a request to help, not a duty
                 help: claims.indexOf("Te pedimos que nos ayudes a reclamarle") >= 0 && claims.indexOf("A tu cargo") < 0,
                 days: claims.indexOf("hace ") >= 0 };
  // a receipt photo from the list: a «receipt» event of that route
  ctx.window.RepartoApp.readReceipt = () => Promise.resolve({ photo: "aGVsbG8=", thumb: "" });
  const input = el({ "data-f5": "photo", "data-stop": 501, "data-route": 55, "data-amount": 25000 });
  input.files = [{ name: "comprobante.jpg", type: "image/jpeg", size: 1000 }];
  fire("change", input);
  await flush();
  const rc = Array.from(ctx.__store.outbox.values()).concat(server.calls.filter((c) => c.url.indexOf("/sync") >= 0)
    .map((c) => c.body.events).reduce((a, b) => a.concat(b), [])).filter((e) => e.type === "receipt")[0] || {};
  out.claimPhoto = { route: rc.route_id, stop: rc.payload && rc.payload.stop_id, amount: rc.payload && rc.payload.transfer_amount };
  // «Ahora no» hides the reminder (never a block)
  tap5("remind-off");
  out.reminderOff = screen(T, "#visitas").indexOf("sin cerrar") < 0;
  // without a route: the card is still there
  out.homeWithoutRoute = ctx.window.RepartoExt.homeHtml().indexOf("Mis comprobantes a reclamar") >= 0;
  // 19.0.1.129.48: the block on the home screens (reparto_v129_48 checks)
  const blockOf = (home) => { ctx.window.RepartoV2.setHome(home); return ctx.window.RepartoExt.homeHtml(); };
  // 501's photo was queued above: its row says so instead of the buttons
  const queuedRow = blockOf(homeV2()).indexOf("Foto guardada en el celular") >= 0;
  const fresh = homeV2();
  fresh.receipts[0] = Object.assign({}, fresh.receipts[0], { stop_id: 510 });
  let blk = blockOf(fresh);
  const rows = (html) => (html.match(/class="v2rec-item"/g) || []).length;
  out.block = { rows: rows(blk), name: blk.indexOf("ALMACÉN SUR") >= 0, amount: blk.indexOf("$ 25.000") >= 0,
                doc: blk.indexOf("Factura 501") >= 0, days: /entregado (hace \d+ días|ayer|hoy)/.test(blk),
                photo: blk.indexOf('data-f5="photo" data-stop="510"') >= 0, queued: queuedRow,
                whatsapp: blk.indexOf("Pedir por WhatsApp") >= 0 && blk.indexOf("wa.me/5492995000222") >= 0,
                resolvedLeftOut: blk.indexOf("KIOSCO NORTE") < 0, all: blk.indexOf('href="#reclamar"') >= 0 };
  const many = homeV2();
  many.charges = [];
  many.receipts = [0, 1, 2, 3, 4].map((i) => Object.assign({}, many.receipts[0], {
    stop_id: 600 + i, name: "CLIENTE " + i, amount: 1000 * (i + 1) }));
  blk = blockOf(many);
  out.blockMany = { rows: rows(blk), more: blk.indexOf("Ver todos (5)") >= 0, first: blk.indexOf("CLIENTE 0") >= 0,
                    hidden: blk.indexOf("CLIENTE 4") < 0 };
  const empty = homeV2();
  empty.receipts = []; empty.charges = []; empty.stale_routes = [];
  blk = blockOf(empty);
  out.blockEmpty = { calm: blk.indexOf("Nada para reclamar") >= 0, rows: rows(blk),
                     noLink: blk.indexOf('href="#reclamar"') < 0 };
  const liability = /a tu cargo|cargo a tu|a tu nombre|tu cuenta|responsabilidad/i;
  const words = { test: (html) => liability.test(html) || /Total/.test(html) };
  const claimsNow = (ctx.window.RepartoV2.setHome(homeV2()), screen(T, "#reclamar"));
  out.blockWording = !words.test(blockOf(homeV2())) && !words.test(blockOf(many)) && !words.test(claimsNow);
  ctx.window.RepartoV2.setHome(homeV2());
  // #4 the driver's own order
  let posted = null;
  server.order = (b) => { posted = b; return { data: { ok: true } }; };
  screen(T, "#orden");
  tap5("down", "", { "data-id": 801 });
  out.orderAfterMove = T.S.route.stops.map((s) => s.id);
  ctx.window.RepartoV2.postOrder(); await flush();
  out.orderPosted = posted && posted.stop_ids;
  await T.download(false); await flush();
  out.orderAfterDownload = T.S.route.stops.map((s) => s.id);

  // ---- 5. the older app's outbox survives the update
  const oldEv = { uuid: "aaaaaaaa-1111-2222-3333-444444444444", type: "visit", route_id: 60, uid: 7,
                  captured_at: "2026-11-25T12:00:00.000Z", key: "visit:801",
                  payload: { visit_uuid: "bbbbbbbb-1111-2222-3333-444444444444", stop_id: 801, outcome: "delivered",
                             returns_answer: "none", returns: [], code: "", without_code: true, cash: 1000,
                             transfer: "no", transfer_amount: 0, receipt_answer: null, no_payment: false, comment: "" } };
  T = freshApp({ app5: true, outbox: [oldEv] });
  T.boot(); await flush();
  out.queueSent = server.calls.some((c) => c.url.indexOf("/sync") >= 0 && c.body.events.some((e) => e.uuid === oldEv.uuid));
  out.queueEmpty = ctx.__store.outbox.size === 0;

  process.stdout.write(JSON.stringify(out));
})().catch((err) => { process.stderr.write(String(err && err.stack || err)); process.exit(1); });
