// Runs the driver app (static/src/reparto/app3.js, plus app4.js for the
// notice) in a Node vm like reparto_v115.js and prints the 19.0.1.129.22
// checks as JSON:
// - a rejected visit resolved by administración: the phone takes the
//   server's visit and the close is no longer blocked;
// - «Entregar igual» sets (not a toggle), «deshacer», and says why before
//   «¿Devolvió algo?»;
// - «Ya estaba pagado»: the payload, the state and the server's warning;
// - app4.js: «Ya está pagado» without a receipt card.
// Used by tests/test_v129_22_reparto_etapa0.py.
//   node reparto_v129_22.js <module root>
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = process.argv[2];
let src = fs.readFileSync(path.join(root, "static/src/reparto/app3.js"), "utf8");
const app4 = fs.readFileSync(path.join(root, "static/src/reparto/app4.js"), "utf8");
const MARK = "  // --------------------------------------------------------------- start";
if (!src.includes(MARK)) throw new Error("start mark not found");
src = src.replace(MARK, [
  "  var ST = globalThis.__store, cp = function (v) { return v === undefined ? undefined : JSON.parse(JSON.stringify(v)); };",
  "  kvGet = function (k) { return Promise.resolve(cp(ST.kv.get(k))); };",
  "  kvSet = function (k, v) { ST.kv.set(k, cp(v)); return Promise.resolve(); };",
  "  kvDel = function (k) { ST.kv.delete(k); return Promise.resolve(); };",
  "  kvKeys = function () { return Promise.resolve(Array.from(ST.kv.keys())); };",
  "  outboxAll = function () { return Promise.resolve(Array.from(ST.outbox.values()).map(cp)); };",
  "  outboxPut = function (ev) { ST.outbox.set(ev.uuid, cp(ev)); return Promise.resolve(); };",
  "  outboxDel = function (id) { ST.outbox.delete(id); return Promise.resolve(); };",
  "  globalThis.__T = { S: S, boot: boot, download: download, syncNow: syncNow, ACTIONS: ACTIONS,",
  "    closeSummary: closeSummary, closeRoute: closeRoute, stopState: stopState, stopById: stopById,",
  "    viewVisit: viewVisit, viewVisits: viewVisits, codeStatus: codeStatus,",
  "    draft: function () { return draft; }, confirm: function () { return confirmState; } };",
  "  return;",
].join("\n") + "\n");

const elements = {};
function element(id) {
  if (!elements[id]) elements[id] = { id: id, innerHTML: "", textContent: "", hidden: true, className: "" };
  return elements[id];
}
const body = { attrs: {}, getAttribute(name) { return this.attrs[name] || null; }, appendChild() {} };
const server = { calls: [], route: null, sync: null };
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
  if (url.indexOf("/reparto/api/sync") === 0) {
    const out = server.sync(call.body);
    return respond(out.status || 200, out.data);
  }
  return Promise.reject(new TypeError("offline"));
}
const noop = () => {};
const ctx = {
  console, Intl, Promise, Math, JSON, Date, encodeURIComponent, Array, Object, Number, String, URL,
  navigator: { onLine: true },
  crypto: require("crypto").webcrypto,
  window: { addEventListener: noop, open: noop, scrollTo: noop },
  document: { addEventListener: noop, getElementById: element, createElement: () => element("new"), body: body,
              visibilityState: "hidden", activeElement: null },
  setInterval: noop, setTimeout: noop, clearTimeout: noop, location: { hash: "" },
  indexedDB: {}, fetch: fakeFetch,
};
ctx.globalThis = ctx;
ctx.self = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(root, "static/src/reparto/claim.js"), "utf8"), ctx);
ctx.window.DistriClaim = ctx.DistriClaim;

function freshApp(kv, outbox, uid) {
  ctx.__store = { kv: new Map(Object.entries(kv || {})), outbox: new Map((outbox || []).map((e) => [e.uuid, e])) };
  body.attrs = uid ? { "data-uid": String(uid) } : {};
  server.calls = [];
  ctx.location.hash = "";
  ctx.window.RepartoExt = undefined;
  vm.runInContext(src, ctx);
  return ctx.__T;
}
const flush = async () => { for (let i = 0; i < 30; i++) await new Promise((r) => setImmediate(r)); };
const stop = (id, extra) => Object.assign({ id: id, seq: 1, order_id: id, order: "S" + id, document: "Factura " + id,
  state: "pending", delivery_state: "pending", loaded: true, partner_id: 9, commercial_partner_id: 9,
  shipping_partner_id: 9, name: "KIOSCO " + id, town: "Centenario", address: "Calle 1", parcels: 2, zone: "",
  parcel: null, lines: [{ id: 1, product: "Producto", qty: 10, uom: "u", unit_price: 100 }],
  order_amount: 1000, previous_debt: 0, paid_elsewhere: 0, retained: "", code_locked: false,
  code_tries_left: 5, code_attempts_recorded: 0, code_salt: "s", code_hash: "h", whatsapp: "",
  whatsapp_confirmed: false, account: true, customer: {}, notice: null, comment: "", depot_note: "",
  red: null, visit: null, payments: [], payments_due: 1000, payments_paid: false }, extra || {});
const route = (stops, rejections) => ({ id: 60, name: "Ruta 60", date: "2026-11-24", trip: "1", state: "on_route",
  zones: "Centro", stops: stops, left: [], expenses: [], receipts: [], rejections: rejections || [] });
const pick = { id: 60, name: "Ruta 60", state: "on_route", state_label: "En reparto", title: "Viaje 1", detail: "24/11" };
const local = (visits, rejected) => ({ routeId: 60, visits: visits, loaded: {}, departed: true, closed: false,
  customers: {}, whatsapp: {}, rejected: rejected || [], expenses: {}, arrivals: {} });

(async () => {
  const out = {};

  // ---- 1. a rejected visit, then resolved on the server
  const phoneVisit = { visit_uuid: "v-old", stop_id: 601, outcome: "delivered", returns_answer: "none", returns: [],
    without_code: true, cash: 0, transfer: "yes", transfer_amount: 492000, receipt_answer: "not_yet",
    no_payment: false, synced: false, error: "El cobro es demasiado." };
  const rejectedEv = { uuid: "ev-1", type: "visit", route_id: 60, key: "visit:601",
    captured_at: "2026-11-24T12:00:00.000Z", payload: { stop_id: 601, visit_uuid: "v-old", transfer_amount: 492000 } };
  const serverVisit = { visit_uuid: "v-new", outcome: "delivered", returns_answer: "none", returns: [],
    code_ok: false, without_code: true, cash: 0, transfer: "no", transfer_amount: 0, receipt_answer: null,
    no_payment: false, already_paid: true, comment: "", synced: true };
  let rejState = "open", served = stop(601);
  server.sync = () => ({ status: 503, data: {} });
  server.route = () => ({ ok: true, user: "Simón", user_id: 7, routes: [pick],
    route: route([served], [{ uuid: "ev-1", state: rejState, resolution: "", type: "visit", stop_id: 601,
                              visit_uuid: "v-old" }]) });
  const kv = () => ({ uid: 7, "u:7:cur": 60, "u:7:route:60": route([stop(601)]),
    "u:7:local:60": local({ 601: phoneVisit }, [{ root: "ev-1", type: "visit", event: rejectedEv,
                                                   error: "El cobro es demasiado.", hidden: false }]) });
  let T = freshApp(kv(), [], 7);
  T.boot(); await flush();
  out.stillOpen = { unsynced: T.closeSummary().unsynced, error: !!T.S.local.visits[601].error,
                    rejected: T.S.local.rejected.length };
  // administración resent it (the script) or resolved it: the server has its visit
  rejState = "resent";
  served = stop(601, { state: "already_paid", delivery_state: "delivered_no_code", visit: serverVisit });
  T = freshApp(kv(), [], 7);
  T.boot(); await flush();
  const v = T.S.local.visits[601];
  out.resolved = { unsynced: T.closeSummary().unsynced, visit_uuid: v.visit_uuid, error: v.error || null,
                   resolved: !!v.resolved, rejected: T.S.local.rejected.length, state: T.stopState(T.stopById(601)),
                   listed: T.viewVisits().indexOf("Administración resolvió esta visita") >= 0 };
  // resolved without a visit on the server: the stop goes back to pending
  rejState = "resolved";
  served = stop(601);
  T = freshApp(kv(), [], 7);
  T.boot(); await flush();
  out.resolvedNoVisit = { visit: T.S.local.visits[601] || null, unsynced: T.closeSummary().unsynced };
  // the close asks the server first: resolved meanwhile, it is no longer blocked
  rejState = "open";
  served = stop(601);
  T = freshApp(kv(), [], 7);
  T.boot(); await flush();
  rejState = "resent";
  served = stop(601, { state: "already_paid", delivery_state: "delivered_no_code", visit: serverVisit });
  const before = server.calls.length;
  await T.closeRoute(false); await flush();
  out.closeRefresh = { asked: server.calls.slice(before).some((c) => c.url.indexOf("/reparto/api/route") === 0),
                       unsynced: T.closeSummary().unsynced,
                       closeQueued: T.S.outbox.some((e) => e.type === "close") ||
                         server.calls.some((c) => c.body && c.body.events && c.body.events.some((e) => e.type === "close")) };

  // ---- 2. «Entregar igual»
  server.route = () => ({ ok: true, user: "Simón", user_id: 7, routes: [pick], route: route([stop(602), stop(603, {
    code_locked: true, code_hash: "" }), stop(604, { payments_paid: true, payments_due: 0,
    payments_paid_how: "Mercado Pago · 20/11" })]) });
  T = freshApp({ uid: 7 }, [], 7);
  T.boot(); await flush();
  ctx.location.hash = "#visita/602";
  const s602 = T.stopById(602);
  T.viewVisit(s602);
  T.ACTIONS["no-code"](null, s602);
  out.noCodeLocked = { on: T.draft().without_code, toast: element("toast").textContent };
  T.ACTIONS["ret-none"]();
  T.ACTIONS["no-code"](null, s602);
  const first = T.draft().without_code;
  T.ACTIONS["no-code"](null, s602);
  out.noCode = { first: first, second: T.draft().without_code, status: T.codeStatus(s602),
                 button: /data-a="no-code"(?![^>]*disabled)/.test(T.viewVisit(s602)) };
  T.ACTIONS["no-code-undo"]();
  out.noCodeUndo = T.draft().without_code;
  // a locked code: the status says «Entregado sin código» once set
  ctx.location.hash = "#visita/603";
  const s603 = T.stopById(603);
  T.viewVisit(s603);
  T.ACTIONS["ret-none"]();
  T.ACTIONS["no-code"](null, s603);
  out.lockedStatus = T.codeStatus(s603);

  // ---- 3. «Ya estaba pagado»
  server.sync = (body) => ({ data: { results: body.events.map((e) => ({ uuid: e.uuid, status: "ok" })) } });
  ctx.location.hash = "#visita/604";
  const s604 = T.stopById(604);
  out.paidView = T.viewVisit(s604).indexOf('data-a="already-paid"') >= 0;
  out.unpaidView = T.viewVisit(s602).indexOf('data-a="already-paid"') >= 0;
  T.viewVisit(s604);
  T.ACTIONS["ret-none"]();
  T.ACTIONS["no-code"](null, s604);
  T.ACTIONS.tr({ getAttribute: () => "yes" }, s604);
  T.draft().transfer_amount = 492000;
  T.ACTIONS["already-paid"](null, s604);
  out.confirmText = (T.confirm() || {}).text || "";
  T.ACTIONS["confirm-yes"]();
  await flush();
  const sent = server.calls.filter((c) => c.url.indexOf("/sync") >= 0)
    .map((c) => c.body.events).reduce((a, b) => a.concat(b), []).filter((e) => e.type === "visit");
  const p = sent.length ? sent[sent.length - 1].payload : {};
  out.alreadyPaid = { already_paid: p.already_paid, cash: p.cash, transfer: p.transfer,
                      transfer_amount: p.transfer_amount, no_payment: p.no_payment,
                      state: T.stopState(s604), summary: T.closeSummary().sum.already_paid };
  // «Guardar visita» with $ 0 on a paid order goes the same way
  T.S.local.visits = {};
  ctx.location.hash = "#visita/604";
  T.viewVisit(s604);
  T.ACTIONS["ret-none"]();
  T.ACTIONS["no-code"](null, s604);
  T.ACTIONS.save(null, s604);
  out.saveZero = (T.confirm() || {}).title || "";
  T.ACTIONS["confirm-no"]();
  // the server found it still owed: recorded as not paid, with its warning
  server.sync = (body) => ({ data: { results: body.events.map((e) => ({ uuid: e.uuid, status: "ok",
    warning: "S604 todavía debe $ 1.000: no figura pagado." })) } });
  T.viewVisit(s604);
  T.ACTIONS["ret-none"]();
  T.ACTIONS["no-code"](null, s604);
  T.ACTIONS["already-paid"](null, s604);
  T.ACTIONS["confirm-yes"]();
  await flush();
  const w = T.S.local.visits[604];
  out.warned = { already_paid: w.already_paid, no_payment: w.no_payment, warning: w.warning,
                 state: T.stopState(s604) };

  // ---- 4. app4.js: «Ya está pagado» without a receipt card
  // the hooks app3.js exposes after its start (cut above)
  ctx.window.RepartoApp = { route: () => null, stopById: () => null, online: () => true, departed: () => true,
    saveRoute: noop, rerender: noop, money: T.money || ((n) => "$ " + n), h: (x) => String(x),
    fetchWithTimeout: () => Promise.reject(new TypeError("offline")), isLoginPage: () => false,
    api: { load: "/reparto/api/load" } };
  vm.runInContext(app4, ctx);
  const ext = ctx.window.RepartoExt;
  out.notice = ext.noticeHtml({ id: 1, payments: [], payments_paid: true, payments_paid_how: "Mercado Pago · 20/11" });
  out.noNotice = ext.noticeHtml({ id: 2, payments: [], payments_paid: false });

  process.stdout.write(JSON.stringify(out));
})().catch((err) => { process.stderr.write(String(err && err.stack || err)); process.exit(1); });
