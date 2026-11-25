// Runs the driver app (static/src/reparto/app3.js, app4.js and app5.js) in a
// Node vm and prints the 19.0.1.129.33 checks as JSON:
// - flag off: «Ubicación y horarios» is the same HTML with and without
//   app5.js (no WhatsApp card);
// - flag on: a saved visit opens again with what was saved and can be
//   changed and saved again (same visit_uuid) while the route is open;
// - a tap the visit screen cannot take says why (held order, route closed
//   with and without «Reabrir la ruta») instead of doing nothing;
// - the customer's WhatsApp on «Ubicación y horarios»: Corregir, a short
//   number refused, Guardar sends the same «whatsapp» event as the old visit.
// Used by tests/test_v129_33_diseno.py.
//   node reparto_v129_33.js <module root>
// (the setup below is the one of reparto_v129_28.js)
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = process.argv[2];
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
  vm.runInContext(app3, ctx);
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

const sentOf = (kind) => server.calls.filter((c) => c.url.indexOf("/sync") >= 0)
  .map((c) => c.body.events).reduce((a, b) => a.concat(b), []).filter((e) => e.type === kind);
const toastText = () => element("toast").textContent;

(async () => {
  const out = {};
  const V2 = { on: false };
  server.route = () => ({ ok: true, user: "Simón", user_id: 7, routes: [pick], route: routeOf(V2.on) });

  // ---- 1. flag off: «Ubicación y horarios» is the same with app5.js
  server.home = () => ({ ok: true, v2: false });
  let base = freshApp({});
  base.boot(); await flush();
  const custWithout = screen(base, "#cliente/801");
  let T = freshApp({ app5: true });
  T.boot(); await flush();
  const custWith = screen(T, "#cliente/801");
  out.flagOffCustomerSame = custWithout === custWith;
  out.flagOffNoWhatsapp = custWith.indexOf("WhatsApp del cliente") < 0 && custWith.indexOf("cwa-") < 0;

  // ---- 2. flag on: a saved visit opens again and can be changed
  V2.on = true;
  server.home = homeV2;
  T = freshApp({ app5: true });
  T.boot(); await flush();
  screen(T, "#visita/801");
  tap5("deliv", "all");
  tap5("pay", "cash");
  tap5("save");
  tap3("confirm-yes");
  await flush();
  const first = sentOf("visit").pop() || { payload: {} };
  let v = screen(T, "#visita/801");
  out.reopenShowsSaved = { pay: T.draft().pay, cash: T.draft().cash,
                           cashOn: /class="opt on" data-a5="pay" data-v="cash"/.test(v),
                           deliveredOn: /class="opt on" data-a5="deliv" data-v="all"/.test(v) };
  tap5("pay", "transfer");
  tap3("rcpt", "not_yet");
  tap5("save");
  tap3("confirm-yes");
  await flush();
  const second = sentOf("visit").pop() || { payload: {} };
  out.reedit = { sameUuid: !!first.payload.visit_uuid && first.payload.visit_uuid === second.payload.visit_uuid,
                 firstCash: first.payload.cash, cash: second.payload.cash, transfer: second.payload.transfer,
                 amount: second.payload.transfer_amount, receipt: second.payload.receipt_answer,
                 local: T.S.local.visits[801] && T.S.local.visits[801].transfer };

  // ---- 3. a tap the screen cannot take says why
  // the order was held after the screen was drawn
  screen(T, "#visita/804");
  T.stopById(804).retained = "Cliente con deuda vieja";
  element("toast").textContent = "";
  tap5("pay", "cash");
  out.retained = { toast: toastText(), draftPay: (T.draft() || {}).pay || null,
                   redrawn: element("app").innerHTML.indexOf("Le toca pagar") < 0 };
  // the route was closed on this phone while the visit was on screen
  screen(T, "#visita/805");
  T.S.local.closed = true;
  element("toast").textContent = "";
  tap5("deliv", "all");
  out.closed = { toast: toastText(), reopen: element("app").innerHTML.indexOf('data-a="reopen-route"') >= 0,
                 title: element("app").innerHTML.indexOf("Ruta cerrada") >= 0 };
  // and when it can no longer be reopened from the phone
  T.S.route.reopen_locked = true;
  element("toast").textContent = "";
  tap5("pay", "cash");
  out.closedLocked = { toast: toastText(), reopen: element("app").innerHTML.indexOf('data-a="reopen-route"') >= 0 };
  T.S.route.reopen_locked = false;
  T.S.local.closed = false;

  // ---- 4. the customer's WhatsApp on «Ubicación y horarios»
  let c = screen(T, "#cliente/802");
  out.waCard = { title: c.indexOf("WhatsApp del cliente") >= 0, number: c.indexOf("299 5000111") >= 0,
                 fix: c.indexOf('data-a5="cwa-edit"') >= 0, confirm: c.indexOf('data-a5="cwa-ok"') >= 0,
                 hours: c.indexOf("Horarios en que recibe") >= 0 };
  tap5("cwa-edit");
  c = element("app").innerHTML;
  out.waEditing = c.indexOf('data-f5="cwa"') >= 0 && c.indexOf('data-a5="cwa-save"') >= 0;
  fire("input", el({ "data-f5": "cwa" }, "299"));
  element("toast").textContent = "";
  tap5("cwa-save");
  out.waShort = toastText();
  fire("input", el({ "data-f5": "cwa" }, "299 4111222"));
  tap5("cwa-save");
  await flush();
  const wa = sentOf("whatsapp").pop() || { payload: {} };
  out.waSent = { partner: wa.payload.partner_id, number: wa.payload.number, action: wa.payload.action,
                 route: wa.route_id };
  c = screen(T, "#cliente/802");
  out.waAfter = c.indexOf("299 4111222") >= 0 && c.indexOf("Confirmado") >= 0;
  // with the route closed the card is not drawn and a stale tap says why
  T.S.local.closed = true;
  element("toast").textContent = "";
  tap5("cwa-edit");
  out.waClosed = toastText();

  process.stdout.write(JSON.stringify(out));
})().catch((err) => { process.stderr.write(String(err && err.stack || err)); process.exit(1); });
