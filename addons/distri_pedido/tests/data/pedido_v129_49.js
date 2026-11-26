// 19.0.1.129.49: /pedido's steps in the browser history
// (static/src/js/pedido_pasos.js) and the device draft keeping the shop's
// name (static/src/js/pedido_borrador.js), against a fake window/history.
// Prints the checks as JSON. Used by tests/test_v129_49_nombre_comercio.py.
//   node pedido_v129_49.js <module root>
"use strict";
const fs = require("fs");
const path = require("path");

const root = process.argv[2];
const read = (p) => fs.readFileSync(path.join(root, p), "utf8");
function load(file, globals) {
  const names = Object.keys(globals);
  new Function(...names, read(file))(...names.map((n) => globals[n]));
}

// a browser history: entries, an index, popstate on back/forward
function fakeWindow(initialState) {
  const listeners = {};
  const win = {
    left: false,
    addEventListener(t, f) { (listeners[t] = listeners[t] || []).push(f); },
    fire(t, ev) { (listeners[t] || []).forEach((f) => f(ev)); },
  };
  const entries = [{ state: initialState === undefined ? null : initialState }];
  let i = 0;
  win.history = {
    get state() { return entries[i].state; },
    pushState(s) { entries.splice(i + 1); entries.push({ state: s }); i += 1; },
    replaceState(s) { entries[i].state = s; },
    back() {
      if (i === 0) { win.left = true; return; }  // leaves the page
      i -= 1; win.fire("popstate", { state: entries[i].state });
    },
    forward() {
      if (i >= entries.length - 1) return;
      i += 1; win.fire("popstate", { state: entries[i].state });
    },
    get length() { return entries.length; },
    get index() { return i; },
  };
  return win;
}

function pasos() {
  const window = {};
  load("static/src/js/pedido_pasos.js", { window });
  const P = window.DNPedidoPasos;
  const out = {};
  const win = fakeWindow();
  const drawn = [];
  const nav = P.crear(win, (n) => drawn.push(n));
  // catalog → «Ver pedido» → «Continuar»: two entries pushed
  nav.ir(2);
  nav.ir(3);
  out.forwardPushes = win.history.length === 3 && drawn.join(",") === "2,3" && nav.actual() === 3;
  // the phone's back on «Tus datos»: «Tu pedido»; again: the catalog; the
  // page is never left on the way
  win.history.back();
  out.backToStep2 = nav.actual() === 2 && drawn[drawn.length - 1] === 2 && !win.left;
  win.history.back();
  out.backToCatalog = nav.actual() === 1 && drawn[drawn.length - 1] === 1 && !win.left;
  // back from the catalog leaves the page as before
  win.history.back();
  out.backFromCatalogLeaves = win.left === true;

  // the page's own buttons go through the history too
  const win2 = fakeWindow();
  const drawn2 = [];
  const nav2 = P.crear(win2, (n) => drawn2.push(n));
  nav2.ir(2);
  nav2.ir(1);  // «Seguir comprando»
  out.buttonBackUsesHistory = nav2.actual() === 1 && win2.history.index === 0 && !win2.left;
  nav2.ir(2);
  out.reforwardReplacesTail = win2.history.length === 2 && win2.history.index === 1;
  nav2.ir(3);
  nav2.ir(2);  // «Volver al pedido»
  out.volverAlPedido = nav2.actual() === 2 && win2.history.index === 1;
  // browser forward again redraws step 3
  win2.history.forward();
  out.forwardRedraws = nav2.actual() === 3 && drawn2[drawn2.length - 1] === 3;

  // the «Ajustes» sheet's own entry is left to ajustes.js
  const win3 = fakeWindow();
  const drawn3 = [];
  const nav3 = P.crear(win3, (n) => drawn3.push(n));
  nav3.ir(2);
  win3.history.pushState({ dn: { d: 1 }, aj: 1 }, "");  // ajustes.js open()
  const before = drawn3.length;
  win3.fire("popstate", { state: { aj: 1 } });
  out.ajustesIgnored = drawn3.length === before && nav3.actual() === 2;
  win3.history.back();  // closing Ajustes
  out.ajustesCloseStays = nav3.actual() === 2 && !win3.left;

  // a reload on step 3: the entry goes back to the catalog
  const win4 = fakeWindow({ dnPaso: 3, dn: { d: 2 } });
  const nav4 = P.crear(win4, () => {});
  out.reloadResets = nav4.actual() === 1 && win4.history.state.dnPaso === 1 && win4.history.state.dn.d === 2;

  // no history API: still draws
  const drawn5 = [];
  const nav5 = P.crear({ addEventListener() {} }, (n) => drawn5.push(n));
  nav5.ir(2); nav5.ir(1);
  out.noHistoryStillWorks = drawn5.join(",") === "2,1";
  out.pasoDe = P.pasoDe(null) === 1 && P.pasoDe({ dnPaso: 2 }) === 2 && P.pasoDe({ dnPaso: 9 }) === 1;
  return out;
}

function borrador() {
  const window = {};
  load("static/src/js/pedido_borrador.js", { window, document: undefined });
  const B = window.DNPedidoBorrador;
  const store = {};
  const local = { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; } };
  const fila = (id, n) => ({ getAttribute: (a) => (a === "data-id" ? String(id) : null), querySelector: () => ({ value: n ? String(n) : "" }) });
  const form = { elements: { nombre: { value: "Ana Pérez" }, comercio: { value: "Kiosco Ana" } },
    querySelectorAll: () => [fila(11, 2)], getAttribute: () => "visitante:1" };
  const key = B.clave(form);
  B.guardar(local, key, form, Date.now());
  const saved = JSON.parse(store[key]);
  const els2 = { nombre: { value: "" }, comercio: { value: "" } };
  const inp = { value: "" };
  B.restaurar({ elements: els2, querySelectorAll: () => [{ getAttribute: (a) => (a === "data-id" ? "11" : null), querySelector: () => inp }] }, saved);
  const src = read("static/src/js/pedido_borrador.js");
  return {
    savesShop: saved.campos.comercio === "Kiosco Ana" && saved.campos.nombre === "Ana Pérez",
    restoresShop: els2.comercio.value === "Kiosco Ana" && els2.nombre.value === "Ana Pérez" && inp.value === "2",
    visitorKey: key === "dn:pedido:borrador:visitante:1",
    submitKeepsDraft: src.indexOf("addEventListener('submit'") < 0,
  };
}

const result = { pasos: pasos(), borrador: borrador() };
process.stdout.write(JSON.stringify(result));
