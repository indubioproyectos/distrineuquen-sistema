// Runs the driver app (static/src/reparto/app2.js) in a Node vm with an
// in-memory phone storage and a scripted server, and prints what happened in
// each 19.0.1.113.0 scenario as JSON. Used by tests/test_v113_reparto.py.
//   node reparto_v113.js <module root>
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = process.argv[2];
let src = fs.readFileSync(path.join(root, "static/src/reparto/app2.js"), "utf8");
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
  "    emptyLocal: emptyLocal };",
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

  // 1. another user on the same phone: the previous user's route is never
  // shown, and their pending change is kept and never sent as the new user
  let T = freshApp({
    uid: 7, "u:7:cur": 10, "u:7:route:10": route(10), "u:7:local:10": { routeId: 10, visits: {}, loaded: {},
      departed: true, closed: false, customers: {}, whatsapp: {}, rejected: [], expenses: {}, arrivals: {} },
  }, [event("a-1", 10, 7)], 8);
  server.route = () => ({ ok: true, user: "Beto", user_id: 8, routes: [pick(20)], route: route(20) });
  server.sync = (b) => ({ data: { ok: true, results: b.events.map((e) => ({ uuid: e.uuid, status: "ok" })) } });
  T.boot(); await flush();
  out.switch = {
    route: T.S.route && T.S.route.id, uid: T.S.uid, outbox: T.S.outbox.length,
    sent: sentUuids(), kept: ctx.__store.outbox.has("a-1"),
    previousKept: !!ctx.__store.kv.get("u:7:route:10"), current: ctx.__store.kv.get("u:8:cur"),
    lastUser: ctx.__store.kv.get("uid"),
  };
  // the cached shell of user 7 while user 9 logged in: the server names 9
  T = freshApp(Object.fromEntries(ctx.__store.kv), Array.from(ctx.__store.outbox.values()), 7);
  server.route = (url) => ({ ok: true, user: "Caro", user_id: 9, routes: [pick(30)], route: route(30),
                             reset: url.indexOf("?id=") > 0, gone_id: url.indexOf("?id=") > 0 ? 10 : undefined });
  server.sync = (b) => {
    if (b.user_id !== 9) return { status: 409, data: { ok: false, user_mismatch: true } };
    return { data: { ok: true, results: b.events.map((e) => ({ uuid: e.uuid, status: "ok" })) } };
  };
  T.boot(); await flush();
  out.cachedShell = { route: T.S.route && T.S.route.id, uid: T.S.uid, kept: ctx.__store.outbox.has("a-1"),
                      mismatch: server.calls.some((c) => c.url.indexOf("/sync") >= 0 && c.body.user_id === 7) };

  // 2. the route on screen is gone with a pending change: the change is kept,
  // the app moves to the current route, the change is sent and rejected
  T = freshApp({
    uid: 7, "u:7:cur": 10, "u:7:route:10": route(10), "u:7:local:10": { routeId: 10, visits: {}, loaded: {},
      departed: true, closed: false, customers: {}, whatsapp: {}, rejected: [], expenses: {}, arrivals: {} },
  }, [event("g-1", 10, 7)], 7);
  let syncOk = false;
  server.route = (url) => (url.indexOf("?id=10") > 0 ?
    { ok: true, user: "Ana", user_id: 7, routes: [pick(11)], route: route(11), reset: true, gone_id: 10 } :
    { ok: true, user: "Ana", user_id: 7, routes: [pick(11)], route: route(11) });
  server.sync = (b) => (syncOk ? { data: { ok: true, results: b.events.map((e) => ({ uuid: e.uuid, status: "error",
    retry: false, error: "Esa ruta no es tuya." })) } } : { status: 503, data: {} });
  // first without the sync answering (no signal for the sync), then with it
  ctx.navigator.onLine = true;
  T.boot(); await flush();
  out.gone = { route: T.S.route && T.S.route.id, pending: T.S.outbox.length,
               banner: T.statusBanner().includes("ya no está en tu reparto"), gone: T.S.gone && T.S.gone.id };
  syncOk = true;
  await T.syncNow(); await flush();
  out.gone.after = { pending: T.S.outbox.length, banner: T.statusBanner().includes("ya llegó al servidor"),
                     rejected: (ctx.__store.kv.get("u:7:local:10") || { rejected: [] }).rejected.length };
  T.ACTIONS["gone-ok"](); await flush();
  out.gone.dismissed = { gone: T.S.gone, local: ctx.__store.kv.has("u:7:local:10") };

  // 3. two routes: «Tus rutas de hoy», each with its own state; one route:
  // the screens stay as they were
  T = freshApp({ uid: 7 }, [], 7);
  server.route = (url) => {
    const id = /id=(\d+)/.test(url) ? +/id=(\d+)/.exec(url)[1] : 11;
    return { ok: true, user: "Ana", user_id: 7, routes: [pick(11), pick(12, "draft")],
             route: route(id, { state: id === 12 ? "draft" : "on_route" }) };
  };
  server.sync = (b) => ({ data: { ok: true, results: b.events.map((e) => ({ uuid: e.uuid, status: "ok" })) } });
  T.boot(); await flush();
  T.S.local.visits[1] = { visit_uuid: "v", synced: false };
  out.picker = { first: T.S.route.id, bar: T.routesBar().includes("Tus rutas de hoy"),
                 list: (T.viewRoutes().match(/data-a="open-route"/g) || []).length };
  ctx.navigator.onLine = false;
  server.sync = () => ({ status: 503, data: {} });
  await T.enqueue("expense", { expense_uuid: "x", kind: "fuel", amount: 5 }, "exp:x");
  T.ACTIONS["open-route"]({ getAttribute: () => "12" }); await flush();
  out.picker.second = { route: T.S.route.id, visits: Object.keys(T.S.local.visits).length,
                        outbox: T.S.outbox.length, outboxRoute: T.S.outbox[0] && T.S.outbox[0].route_id };
  T.ACTIONS["open-route"]({ getAttribute: () => "11" }); await flush();
  out.picker.back = { route: T.S.route.id, visits: Object.keys(T.S.local.visits).length };
  T.S.otherRoutes = [pick(11)];
  out.picker.single = T.routesBar();

  // 4. a phone with the older app's state: nothing is sent before the
  // server names the user; then it is that user's
  T = freshApp({ route: route(40), local: { routeId: 40, visits: {}, loaded: {}, departed: true, closed: false,
    customers: {}, whatsapp: {}, rejected: [], expenses: {}, arrivals: {} }, gpsNotice: "1" },
  [Object.assign(event("l-1", 40), { uid: undefined })], null);
  const order = [];
  server.route = () => { order.push("route"); return { ok: true, user: "Ana", user_id: 5, routes: [pick(40)], route: route(40) }; };
  server.sync = (b) => { order.push("sync:" + b.user_id); return { data: { ok: true, results: b.events.map((e) => ({ uuid: e.uuid, status: "ok" })) } }; };
  T.boot(); await flush();
  out.legacy = { order: order, uid: ctx.__store.kv.get("uid"), oldKeys: ctx.__store.kv.has("route") || ctx.__store.kv.has("local"),
                 route: ctx.__store.kv.get("u:5:cur"), notice: ctx.__store.kv.get("u:5:gpsNotice"),
                 outbox: ctx.__store.outbox.size };

  process.stdout.write(JSON.stringify(out));
})().catch((err) => { process.stderr.write(String(err && err.stack || err)); process.exit(1); });
