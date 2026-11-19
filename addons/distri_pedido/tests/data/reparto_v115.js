// Runs the driver app (static/src/reparto/app3.js) in a Node vm like
// reparto_v113.js and prints the 19.0.1.115.0 checks as JSON: the amounts
// (parseMoney, money, moneyRaw) and a route reopened by administración.
// Used by tests/test_v115.py.
//   node reparto_v115.js <module root>
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = process.argv[2];
let src = fs.readFileSync(path.join(root, "static/src/reparto/app3.js"), "utf8");
const MARK = "  // --------------------------------------------------------------- start";
if (!src.includes(MARK)) throw new Error("start mark not found");
// the phone's IndexedDB as two maps (structured clone = JSON round trip)
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
  "    routesBar: routesBar, viewRoutes: viewRoutes, statusBanner: statusBanner, enqueue: enqueue,",
  "    emptyLocal: emptyLocal, parseMoney: parseMoney, money: money, moneyRaw: moneyRaw };",
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
  console, Intl, Promise, Math, JSON, Date, encodeURIComponent, Array, Object, Number, String,
  navigator: { onLine: true },
  crypto: require("crypto").webcrypto,
  window: { addEventListener: noop, open: noop, scrollTo: noop },
  document: { addEventListener: noop, getElementById: element, createElement: () => element("new"), body: body },
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
  vm.runInContext(src, ctx);
  return ctx.__T;
}
const flush = async () => { for (let i = 0; i < 30; i++) await new Promise((r) => setImmediate(r)); };
const route = (id, extra) => Object.assign({ id: id, name: "Ruta " + id, date: "2026-11-19", trip: "1",
  state: "on_route", zones: "Centro", stops: [], left: [], expenses: [], rejections: [] }, extra || {});
const pick = (id, state) => ({ id: id, name: "Ruta " + id, state: state || "on_route",
  state_label: state === "draft" ? "Asignada" : "En reparto", title: "Viaje " + id, detail: "19/11" });
const event = (uuid, routeId, uid) => ({ uuid: uuid, type: "expense", route_id: routeId, uid: uid,
  captured_at: "2026-11-19T12:00:00.000Z", payload: { expense_uuid: uuid, kind: "fuel", amount: 10 }, key: null });
const sentUuids = () => server.calls.filter((c) => c.url.indexOf("/sync") >= 0)
  .map((c) => c.body.events.map((e) => e.uuid)).reduce((a, b) => a.concat(b), []);

(async () => {
  const out = {};
  let T = freshApp({ uid: 7 }, [], 7);
  const inputs = ["$ 25.00", "$ 14.307.60", "$ 14.307,60", "14307.6", "14,307.60", "25.000",
                  "25,000", "1.500", "1500,5", "$ 1.234.567", "0,50", "", "abc", "$ 12.345"];
  out.parse = {};
  inputs.forEach((t) => { const v = T.parseMoney(t); out.parse[t] = Number.isNaN(v) ? "NaN" : v; });
  out.money = [0, 25, 14307.6, 1234567.891, 0.5, -5].map(T.money);
  out.raw = [14307.6, 25, 0].map(T.moneyRaw);
  // what money() shows, parsed back, is the same amount
  out.roundTrip = [14307.6, 25, 1500.05, 999999.99].every((n) => T.parseMoney(T.money(n)) === n);

  // a route closed on the phone that administración reopened: open again
  const local = (closed) => ({ routeId: 50, visits: {}, loaded: {}, departed: true, closed: closed,
    customers: {}, whatsapp: {}, rejected: [], expenses: {}, arrivals: {} });
  server.sync = () => ({ status: 503, data: {} });
  let state = "on_route";
  server.route = () => ({ ok: true, user: "Ana", user_id: 7, routes: [pick(50)],
                          route: route(50, { state: state }) });
  T = freshApp({ uid: 7, "u:7:cur": 50, "u:7:route:50": route(50, { state: "closed" }),
                 "u:7:local:50": local(true) }, [], 7);
  T.boot(); await flush();
  out.reopened = { closed: T.S.local.closed, route: T.S.route && T.S.route.id };
  // closed on the phone with the close still to send: stays closed
  T = freshApp({ uid: 7, "u:7:cur": 50, "u:7:route:50": route(50), "u:7:local:50": local(true) },
               [Object.assign(event("c-1", 50, 7), { type: "close", payload: {} })], 7);
  T.boot(); await flush();
  out.pendingClose = { closed: T.S.local.closed, outbox: T.S.outbox.length };
  // the server says closed: closed
  state = "closed";
  T = freshApp({ uid: 7, "u:7:cur": 50, "u:7:route:50": route(50), "u:7:local:50": local(false) }, [], 7);
  T.boot(); await flush();
  out.serverClosed = { closed: T.S.local.closed };

  process.stdout.write(JSON.stringify(out));
})().catch((err) => { process.stderr.write(String(err && err.stack || err)); process.exit(1); });
