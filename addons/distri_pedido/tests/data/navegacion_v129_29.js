// 19.0.1.129.29 (#100): «Atrás» goes UP one level in the three hash apps
// that used to add history: reparto/app3.js (and app5.js, drawn inside it),
// despacho/despacho.js and cuentas/cuentas_app.js. Runs each file's
// «nav-up» block (and app3.js's go()) against a fake browser history and
// prints the checks as JSON. Used by tests/test_v129_29_navegacion.py and
// tests/test_v129_32_correcciones.py.
//   node navegacion_v129_29.js <module root>
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = process.argv[2];
const read = (p) => fs.readFileSync(path.join(root, p), "utf8");

function block(src, start, end) {
  const i = src.indexOf(start);
  const j = src.indexOf(end, i);
  if (i < 0 || j < 0) throw new Error("block not found: " + start);
  return src.slice(i, j + end.length);
}

// A browser tab with same-document hash entries only.
// 19.0.1.129.32: initialState is what history.state already holds when the
// page loads (it survives reloads); with deferBack, back() waits for flush()
// like a real browser does.
function browser(initialHash, initialState, deferBack) {
  const entries = [{ hash: initialHash || "", state: initialState || null }];
  let pending = 0;
  let index = 0;
  const listeners = { hashchange: [], click: [] };
  const fire = (type, ev) => listeners[type].forEach((fn) => fn(ev));
  const url = (hash) => "https://lab.test/app" + (hash ? "#" + hash : "");
  function moveTo(i, oldHash) {
    index = i;
    if (entries[index].hash !== oldHash) {
      fire("hashchange", { oldURL: url(oldHash), newURL: url(entries[index].hash) });
    }
  }
  const history = {
    get state() { return entries[index].state; },
    get length() { return entries.length; },
    replaceState(state) { entries[index].state = state; },
    back() {
      if (deferBack) { pending++; return; }
      if (index > 0) moveTo(index - 1, entries[index].hash);
    },
  };
  const location = {
    get hash() { return entries[index].hash ? "#" + entries[index].hash : ""; },
    set hash(value) {
      const hash = String(value).replace(/^#/, "");
      const old = entries[index].hash;
      if (hash === old) return;
      entries.splice(index + 1);
      entries.push({ hash: hash, state: null });
      moveTo(entries.length - 1, old);
    },
    replace(value) {
      const hash = String(value).replace(/^#/, "");
      const old = entries[index].hash;
      entries[index] = { hash: hash, state: null };
      moveTo(index, old);
    },
  };
  return {
    history, location, entries, listeners,
    index: () => index,
    backCalls: () => pending,
    flush() {
      const n = pending; pending = 0;
      for (let i = 0; i < n; i++) if (index > 0) moveTo(index - 1, entries[index].hash);
    },
    // the phone's «Atrás»: always the previous entry
    phoneBack() { if (index > 0) moveTo(index - 1, entries[index].hash); },
    window: { addEventListener: (t, fn) => listeners[t].push(fn), scrollTo() {} },
    document: { addEventListener: (t, fn) => listeners[t].push(fn) },
    clickBack(href) {
      let prevented = false;
      const a = { getAttribute: (n) => (n === "href" ? href : null) };
      const ev = { target: { closest: (sel) => (sel === "a.back" ? a : null) },
        preventDefault() { prevented = true; }, defaultPrevented: false };
      fire("click", ev);
      return prevented;
    },
    // what the phone back does: the previous entry, or leaving the page
    pressesToLeave() { return index + 1; },
  };
}

function load(file, initialHash, extra, initialState, deferBack) {
  const b = browser(initialHash, initialState, deferBack);
  const src = read(file);
  let code = block(src, "  // nav-up:start", "  // nav-up:end");
  if (extra) code += "\n" + extra(src);
  const ctx = Object.assign({ history: b.history, location: b.location, window: b.window,
    document: b.document, Object, String, setTimeout: () => 0 }, (extra && extra.globals) || {});
  vm.createContext(ctx);
  vm.runInContext(code + "\n;globalThis.__up = goUp;" +
    (extra ? "globalThis.__go = typeof go === 'function' ? go : null;" : ""), ctx);
  b.ctx = ctx;
  return b;
}

const out = {};
function check(name, value) { out[name] = value; }

// ------------------------------------------------------------- despacho
{
  // list -> route (a link pushes) -> «‹ Rutas»: back to the list, no new entry
  const b = load("static/src/despacho/despacho.js", "");
  b.location.hash = "#ruta/5";
  b.ctx.__up("");
  check("despacho_back_from_opened_route", { hash: b.location.hash, index: b.index(),
    leave: b.pressesToLeave() });
  // opened again and back again: still one level
  b.location.hash = "#ruta/6";
  b.ctx.__up("");
  check("despacho_twice", { hash: b.location.hash, leave: b.pressesToLeave() });
  // a route opened straight from a link (WhatsApp, Inicio): «‹ Rutas» replaces
  const c = load("static/src/despacho/despacho.js", "ruta/9");
  c.ctx.__up("");
  check("despacho_back_from_direct_route", { hash: c.location.hash, entries: c.entries.length,
    leave: c.pressesToLeave() });
}

// --------------------------------------------------------------- cuentas
{
  const b = load("static/src/cuentas/cuentas_app.js", "");
  b.location.hash = "#cuenta/12";              // a debtor
  b.location.hash = "#cuenta/12/mov/inv-3";     // a statement row (sheet)
  b.ctx.__up("#cuenta/12");                     // closing the row (closeLine)
  const rowClosed = { hash: b.location.hash, index: b.index() };
  const prevented = b.clickBack("#");           // «‹ Deudores»
  check("cuentas_row_then_debtors", { row: rowClosed, prevented: prevented, hash: b.location.hash,
    leave: b.pressesToLeave() });
  // transfers -> one receipt -> close -> «‹ Deudores» from an account opened there
  const t = load("static/src/cuentas/cuentas_app.js", "");
  t.location.hash = "#transferencias";
  t.location.hash = "#transferencias/40";
  t.ctx.__up("#transferencias");
  t.location.hash = "#cuenta/7";                // «Ver estado de cuenta»
  t.clickBack("#transferencias");               // «‹ Transferencias»
  check("cuentas_transfers", { hash: t.location.hash, leave: t.pressesToLeave() });
  // an account opened from a link: «‹ Deudores» replaces it with the list
  const d = load("static/src/cuentas/cuentas_app.js", "cuenta/3");
  d.clickBack("#");
  check("cuentas_direct_account", { hash: d.location.hash, entries: d.entries.length,
    leave: d.pressesToLeave() });
  // links that are not «back» are left alone
  check("cuentas_other_links", { prevented: d.clickBack("/tablero") });
}

// ------------------------------------------------------------ reparto (app3)
{
  const goSrc = (src) => block(src, "  function go(hash) {", "\n  }\n");
  goSrc.globals = {};
  const S = { local: { departed: true, closed: false } };
  const extra = Object.assign((src) => "var draft, custDraft, expDraft, confirmState = null;" +
    "function closeConfirm() {} function render() { globalThis.__renders++; }\n" + goSrc(src),
  { globals: { S: S, __renders: 0 } });
  const b = load("static/src/reparto/app3.js", "visitas", extra);
  b.location.hash = "#visita/701";              // a visit from the list
  b.clickBack("#visitas");                      // «‹ Visitas»
  check("reparto_back_from_visit", { hash: b.location.hash, leave: b.pressesToLeave() });
  b.location.hash = "#visita/702";
  b.location.hash = "#cuenta/702";              // «Ver cuenta»
  b.clickBack("#visita/702");                   // «‹ Visita»
  b.clickBack("#visitas");                      // «‹ Visitas»
  check("reparto_two_levels", { hash: b.location.hash, leave: b.pressesToLeave() });
  // saving a visit returns to the list with go("#visitas"): no new entry
  b.location.hash = "#visita/703";
  b.ctx.__go("#visitas");
  check("reparto_go_after_saving", { hash: b.location.hash, leave: b.pressesToLeave() });
  // the home screen "" and "#visitas" are the same list once the driver left
  const h = load("static/src/reparto/app3.js", "", extra);
  h.location.hash = "#visita/9";
  h.clickBack("#visitas");
  check("reparto_home_is_the_list", { hash: h.location.hash, leave: h.pressesToLeave() });
  // a forward step (salida -> visitas) still adds its entry
  const f = load("static/src/reparto/app3.js", "carga", extra);
  f.ctx.__go("#salida");
  check("reparto_forward_go", { hash: f.location.hash, entries: f.entries.length });
  // 19.0.1.129.32: «Control de carga» is the entry the app loads on.
  // «Salir a repartir», the phone's «Atrás» to tick a parcel, and
  // «Salir a repartir» again must open the departure screen again.
  const loadExtra = (S2) => Object.assign((src) => "var draft, custDraft, expDraft, confirmState = null;" +
    "function closeConfirm() {} function render() { globalThis.__renders++; }\n" + goSrc(src),
  { globals: { S: S2, __renders: 0 } });
  const notDeparted = { local: { departed: false, closed: false } };
  const d = load("static/src/reparto/app3.js", "", loadExtra(notDeparted));
  d.ctx.__go("#salida");
  const first = { hash: d.location.hash, entries: d.entries.length };
  d.phoneBack();
  const afterBack = { hash: d.location.hash, dnFrom: d.history.state && d.history.state.dnFrom };
  d.ctx.__go("#salida");
  check("reparto_depart_after_phone_back", { first: first, afterBack: afterBack,
    hash: d.location.hash, entries: d.entries.length, index: d.index() });
  // a stale mark left in history.state by 129.29-129.31 (it survives a
  // reload) is cleared when the app loads
  const r = load("static/src/reparto/app3.js", "", loadExtra(notDeparted), { dnFrom: "salida" });
  r.ctx.__go("#salida");
  check("reparto_depart_stale_state", { hash: r.location.hash, entries: r.entries.length });
  // even with a stale mark, go() never steps back to a forward screen
  const s2 = load("static/src/reparto/app3.js", "carga", loadExtra(notDeparted));
  s2.history.replaceState({ dnFrom: "salida" });
  s2.ctx.__go("#salida");
  check("reparto_forward_never_back", { hash: s2.location.hash, entries: s2.entries.length });
  // a double tap on «Guardar» steps back once, not twice
  const t = load("static/src/reparto/app3.js", "visitas", extra, null, true);
  t.location.hash = "#visita/704";
  t.ctx.__go("#visitas");
  t.ctx.__go("#visitas");
  const calls = t.backCalls();
  t.flush();
  check("reparto_double_tap_save", { backCalls: calls, hash: t.location.hash,
    leave: t.pressesToLeave() });
  // app5.js «Mis comprobantes a reclamar» uses the same back links
  const app5 = read("static/src/reparto/app5.js");
  check("app5_back_links", (app5.match(/<a class="back" href="/g) || []).length);
}

process.stdout.write(JSON.stringify(out));
