// Runs the driver app (static/src/reparto/app3.js, plus app4.js) in a Node
// vm like reparto_v129_22.js and prints the 19.0.1.129.25 checks as JSON:
// - «Ya estaba pagado · a confirmar»: the button, the confirmation, the
//   payload, the list, the close summary and app4.js's notice and poll;
// - «Reabrir la ruta»: the button, the POST, the refusal once the cash was
//   counted, and a route reopened by administración (onRouteState).
// Used by tests/test_v129_25_reparto.py.
//   node reparto_v129_25.js <module root>
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
  "    viewClose: viewClose, viewClosed: viewClosed, onRouteState: onRouteState, money: money,",
  "    draft: function () { return draft; }, confirm: function () { return confirmState; } };",
  "  return;",
].join("\n") + "\n");

const elements = {};
function element(id) {
  if (!elements[id]) elements[id] = { id: id, innerHTML: "", textContent: "", hidden: true, className: "" };
  return elements[id];
}
const body = { attrs: {}, getAttribute(name) { return this.attrs[name] || null; }, appendChild() {} };
const server = { calls: [], route: null, sync: null, reopen: null };
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
  if (url.indexOf("/reparto/api/reopen") === 0) {
    const out = server.reopen(call.body);
    return respond(out.status || 200, out.data);
  }
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

  // ---- 1. «Ya estaba pagado · a confirmar»
  const wait = { receipt_id: 31, state: "sin_acreditar", amount: 1000, when_label: "10:05", by: "el cliente",
    channel: "order", channel_label: "página del pedido", audit_state: "", verified: false, has_file: true };
  let routeState = "on_route";
  const stops = () => [stop(701, { payments: [wait], payments_due: 1000, payments_to_confirm: true,
    payments_to_confirm_amount: 1000 }), stop(702), stop(703, { payments: [wait], payments_due: 1000,
    payments_to_confirm: true, payments_to_confirm_amount: 1000 })];
  server.route = () => ({ ok: true, user: "Simón", user_id: 7, routes: [pick],
    route: Object.assign(route(stops()), { state: routeState }) });
  server.sync = (body) => ({ data: { results: body.events.map((e) => ({ uuid: e.uuid, status: "ok" })) } });
  let T = freshApp({ uid: 7 }, [], 7);
  T.boot(); await flush();
  ctx.location.hash = "#visita/701";
  const s701 = T.stopById(701);
  out.confirmView = T.viewVisit(s701);
  out.confirmButton = out.confirmView.indexOf('data-a="already-paid"') >= 0;
  out.noButton = T.viewVisit(T.stopById(702)).indexOf('data-a="already-paid"') >= 0;
  T.viewVisit(s701);
  T.ACTIONS["ret-none"]();
  T.ACTIONS["no-code"](null, s701);
  T.ACTIONS["already-paid"](null, s701);
  out.confirmTitle = (T.confirm() || {}).title || "";
  out.confirmText = (T.confirm() || {}).text || "";
  T.ACTIONS["confirm-yes"]();
  await flush();
  const sent = server.calls.filter((c) => c.url.indexOf("/sync") >= 0)
    .map((c) => c.body.events).reduce((a, b) => a.concat(b), []).filter((e) => e.type === "visit");
  const p = sent.length ? sent[sent.length - 1].payload : {};
  out.sent = { already_paid: p.already_paid, to_confirm: p.to_confirm, cash: p.cash, transfer: p.transfer,
               no_payment: p.no_payment };
  out.state = T.stopState(s701);
  out.list = T.viewVisits();
  const sum = T.closeSummary();
  out.summary = { already_paid: sum.sum.already_paid, not_paid: sum.sum.not_paid, toConfirm: sum.toConfirm };
  out.closeView = T.viewClose();
  // «Guardar visita» with $ 0 points at the button, never saves «No pagó» by itself
  ctx.location.hash = "#visita/703";
  const s703 = T.stopById(703);
  T.viewVisit(s703);
  T.ACTIONS["ret-none"]();
  T.ACTIONS["no-code"](null, s703);
  T.ACTIONS.save(null, s703);
  out.saveZero = element("toast").textContent;

  // app4.js: the notice and the poll
  const polledStop = stop(702);
  ctx.window.RepartoApp = { route: () => ({ id: 60 }), stopById: (id) => (+id === 702 ? polledStop : null),
    online: () => true, departed: () => true, saveRoute: noop, rerender: noop, money: T.money, h: (x) => String(x),
    fetchWithTimeout: () => Promise.reject(new TypeError("offline")), isLoginPage: () => false,
    api: { load: "/reparto/api/load" } };
  vm.runInContext(app4, ctx);
  const ext = ctx.window.RepartoExt;
  out.notice = ext.noticeHtml({ id: 1, payments: [wait], payments_paid: false, payments_to_confirm: true });
  ext.onLoadPoll({ payments: { 702: { payments: [wait], due: 1000, paid: false, paid_how: "", to_confirm: true,
                                      to_confirm_amount: 1000 } } });
  out.polled = polledStop.payments_to_confirm === true && polledStop.payments_to_confirm_amount === 1000;

  // ---- 2. «Reabrir la ruta»
  routeState = "closed";
  T = freshApp({ uid: 7 }, [], 7);
  T.boot(); await flush();
  T.S.local.closed = true;
  T.S.route.reopen_locked = false;
  out.reopenButton = T.viewClosed().indexOf('data-a="reopen-route"') >= 0;
  T.S.route.reopen_locked = true;
  out.reopenLockedButton = T.viewClosed().indexOf('data-a="reopen-route"') >= 0;
  T.S.route.reopen_locked = false;
  let posted = null;
  server.reopen = (body) => { posted = body; routeState = "on_route"; return { data: { ok: true, state: "on_route" } }; };
  T.ACTIONS["reopen-route"]();
  T.ACTIONS["confirm-yes"]();
  await flush();
  out.reopen = { posted: posted, closed: T.S.local.closed };
  // refused: the cash was counted
  routeState = "closed";
  T = freshApp({ uid: 7 }, [], 7);
  T.boot(); await flush();
  T.S.local.closed = true;
  server.reopen = () => ({ status: 409, data: { ok: false, locked: true, error:
    "Ya se cargó el efectivo contado de Ruta 60: solo caja o gerencia reabren la ruta. Pediles que la reabran." } });
  T.ACTIONS["reopen-route"]();
  T.ACTIONS["confirm-yes"]();
  await flush();
  out.reopenRefused = { toast: element("toast").textContent, locked: !!T.S.route.reopen_locked,
                        closed: T.S.local.closed };
  // reopened by administración: the poll hands the state to app3.js
  T.S.route.reopen_locked = false;
  routeState = "on_route";
  T.onRouteState("on_route");
  await flush();
  out.byOthers = { closed: T.S.local.closed };

  process.stdout.write(JSON.stringify(out));
})().catch((err) => { process.stderr.write(String(err && err.stack || err)); process.exit(1); });
