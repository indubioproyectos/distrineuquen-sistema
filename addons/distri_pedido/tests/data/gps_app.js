// Runs the driver app (static/src/reparto/app2.js) in a Node vm with a fake
// GPS and a fake phone storage, and prints what happened in each scenario as
// JSON. Used by tests/test_reparto_gps.py.
//   node gps_app.js <module root>
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = process.argv[2];
// 19.0.1.113.0: the page loads app2.js (app.js stays one version)
let src = fs.readFileSync(path.join(root, "static/src/reparto/app2.js"), "utf8");
const MARK = "  // --------------------------------------------------------------- start";
if (!src.includes(MARK)) throw new Error("start mark not found");
src = src.replace(MARK, [
  "  kvSet = function () { return Promise.resolve(); };",
  "  outboxPut = function () { return Promise.resolve(); };",
  "  outboxDel = function () { return Promise.resolve(); };",
  "  syncNow = function () { return Promise.resolve(); };",
  "  download = function () { return Promise.resolve(); };",
  "  globalThis.__T = { S: S, ACTIONS: ACTIONS, emptyLocal: emptyLocal, viewVisit: viewVisit,",
  "    setDraft: function (d) { draft = d; }, gps: function () { return gpsState; } };",
  "  return;",
].join("\n") + "\n");

const elements = {};
function element(id) {
  if (!elements[id]) elements[id] = { id: id, innerHTML: "", textContent: "", hidden: true, className: "" };
  return elements[id];
}
const geo = { mode: "ok", calls: 0, options: null };
const navigator = {
  onLine: false,
  geolocation: {
    getCurrentPosition(ok, fail, options) {
      geo.calls += 1; geo.options = options;
      if (geo.mode === "ok") {
        ok({ coords: { latitude: -38.9516, longitude: -68.0591, accuracy: 12.4 },
             timestamp: Date.parse("2026-10-22T15:00:00Z") });
      } else {
        fail({ code: geo.mode === "denied" ? 1 : 2 });
      }
    },
  },
};
const noop = () => {};
const ctx = {
  console, navigator, Intl, Promise, Math, JSON, Date, encodeURIComponent,
  crypto: require("crypto").webcrypto,
  window: { addEventListener: noop, open: noop, scrollTo: noop },
  document: { addEventListener: noop, getElementById: element, createElement: () => element("new"),
              body: { appendChild: noop } },
  setInterval: noop, setTimeout: noop, clearTimeout: noop, location: { hash: "#visitas" },
  indexedDB: {}, fetch: () => Promise.reject(new TypeError("offline")),
};
ctx.globalThis = ctx;
ctx.self = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(root, "static/src/reparto/claim.js"), "utf8"), ctx);
ctx.window.DistriClaim = ctx.DistriClaim;
vm.runInContext(src, ctx);
const T = ctx.__T;

const stop = { id: 7, order: "S00001", name: "Almacén de Práctica", address: "Calle 1", parcels: 1,
  lines: [], order_amount: 1000, previous_debt: 0, partner_id: 3, commercial_partner_id: 3,
  shipping_partner_id: 3, document: "", code_hash: "", code_salt: "", customer: {}, red: null };

function reset(gps) {
  T.S.route = { id: 1, name: "R", state: "on_route", stops: [stop], claim_texts: {}, gps: gps };
  T.S.local = T.emptyLocal(1);
  T.S.local.departed = true;
  T.S.outbox = [];
  T.S.gpsNotice = null;
  T.setDraft(null);
  ctx.location.hash = "#visita/7";
}
const flush = () => new Promise((r) => setImmediate(r));
const sheet = () => element("gps-sheet").innerHTML;
const GPS = { required: true, accuracy_m: 100, retention_months: 12, notice_version: "1",
              notice_accepted: true };

(async () => {
  const out = {};

  // 1. required and denied: «Llegué» waits, nothing is queued
  reset(GPS); geo.mode = "denied";
  out.gate = T.viewVisit(stop).includes('data-a="arrive"');
  T.ACTIONS.arrive(null, stop); await flush();
  out.blocked = { queued: T.S.outbox.length, error: (T.gps() || {}).error || null,
                  retry: sheet().includes("gps-retry"), help: sheet().includes("Permisos") };

  // 2. «Reintentar» with the GPS back, offline: the event carries the position
  geo.mode = "ok";
  T.ACTIONS["gps-retry"](); await flush(); await flush();
  const arrive = T.S.outbox[0] || {};
  out.arrive = { type: arrive.type, gps: (arrive.payload || {}).gps || null,
                 sheetClosed: T.gps() === null, options: geo.options };

  // 3. closing the visit while offline: «No se entregó» with the position
  T.setDraft(null);
  T.viewVisit(stop);
  T.ACTIONS.nd(null, stop);
  T.ACTIONS["save-nd"](null, stop); await flush(); await flush();
  const visit = T.S.outbox.filter((e) => e.type === "visit")[0] || {};
  out.visit = { gps: (visit.payload || {}).gps || null, online: navigator.onLine };

  // 4. not required: a failed GPS does not stop the action
  reset(Object.assign({}, GPS, { required: false })); geo.mode = "unavailable";
  T.ACTIONS.arrive(null, stop); await flush();
  out.optional = { queued: T.S.outbox.length,
                   gps: ((T.S.outbox[0] || {}).payload || {}).gps || null };

  // 5. the notice comes first, once; accepting it is sent to the server
  reset(Object.assign({}, GPS, { notice_accepted: false })); geo.mode = "ok";
  T.ACTIONS.arrive(null, stop); await flush();
  out.notice = { shown: !!(T.gps() || {}).notice, text: sheet().includes("cuatro momentos"),
                 queuedBefore: T.S.outbox.length };
  T.ACTIONS["gps-accept"](); await flush(); await flush();
  out.notice.types = T.S.outbox.map((e) => e.type);

  // 6. before «Salir a repartir» nothing is captured for a visit
  reset(GPS); T.S.local.departed = false; geo.calls = 0;
  T.ACTIONS.arrive(null, stop); await flush();
  out.inactive = { calls: geo.calls, gps: ((T.S.outbox[0] || {}).payload || {}).gps === undefined };

  process.stdout.write(JSON.stringify(out));
})().catch((err) => { process.stderr.write(String(err && err.stack || err)); process.exit(1); });
