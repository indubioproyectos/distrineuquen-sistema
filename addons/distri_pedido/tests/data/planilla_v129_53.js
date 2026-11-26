// 19.0.1.129.53: «Nueva venta» on the PC (static/src/nueva_venta/planilla.js)
// and the PC switch of the phone page (views/nueva_venta_app.xml, script
// #nv-vista). Prints the checks as JSON; used by
// tests/test_v129_53_planilla.py.
//   node planilla_v129_53.js <module root>
// - the keyboard line entry: type → list → ↑ ↓ → Enter → quantity → Enter →
//   a new empty line; Supr / ⌫ on an empty line; the same product twice;
// - search (accents, several words, ranking), totals, «a presupuestar»;
// - shortcuts (Alt+1..4, Ctrl+Enter, Alt+C/P/N) and amounts;
// - where a server error is shown;
// - the layout chosen per device and remembered (localStorage), and the
//   page's own shortcuts on a stub DOM.
"use strict";
const fs = require("fs");
const path = require("path");

const root = process.argv[2];
const read = (p) => fs.readFileSync(path.join(root, p), "utf8");
function load(file, globals) {
  const names = Object.keys(globals);
  new Function(...names, read(file))(...names.map((n) => globals[n]));
}
function memoryStore(init) {
  const store = Object.assign({}, init || {});
  return {
    store,
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
  };
}
const out = {};
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// ------------------------------------------------------------ the pure core

const window = {};
load("static/src/nueva_venta/planilla.js", { window, document: undefined });
const P = window.DNPlanilla;
const productos = [
  { id: 1, nombre: "Gaseosa Cola 2,25 L", seccion: "Bebidas", unidad: "Pack", contiene: 6, precio: 9000 },
  { id: 2, nombre: "Galletitas Dulces", seccion: "Almacén", unidad: "Unidad", contiene: 0, precio: 1200 },
  { id: 3, nombre: "Agua Mineral", seccion: "Bebidas", unidad: "Pack", contiene: 6, precio: 5000 },
  { id: 4, nombre: "Cigarrillos Rubios", seccion: "Lista 3", unidad: "Cartón", contiene: 0, precio: 0 },
  { id: 5, nombre: "Jabón en polvo", seccion: "Limpieza", unidad: "Unidad", contiene: 0, precio: 3100 },
];

out.searchPrefixFirst = P.buscarProductos(productos, "ga")[0].id === 1 &&
  P.buscarProductos(productos, "ga").map((p) => p.id).join() === "1,2,4";
out.searchAccentsAndCase = P.buscarProductos(productos, "JABON")[0].id === 5 &&
  P.buscarProductos(productos, "almacen")[0].id === 2;
out.searchSeveralWords = same(P.buscarProductos(productos, "beb agua").map((p) => p.id), [3]);
out.searchEmpty = P.buscarProductos(productos, "  ").length === 0;
out.searchLimit = P.buscarProductos(Array.from({ length: 40 }, (_, i) => ({ id: i, nombre: "Item " + i })), "item").length === 12;

// type → list → ↑ ↓ → Enter → quantity → Enter → new line
const L = new P.Lineas(productos);
out.startsWithOneEmptyLine = L.filas.length === 1 && same(L.foco, { i: 0, campo: "prod" });
L.escribir(0, "a");
const abierto = L.drop.abierto && L.drop.items.length > 1;
L.tecla("ArrowDown");
const bajo = L.drop.idx === 1;
L.tecla("ArrowUp");
out.dropdownArrows = abierto && bajo && L.drop.idx === 0;
L.escribir(0, "gas");
out.enterPicksAndJumpsToQuantity = L.tecla("Enter") === true && L.filas[0].pid === 1 &&
  L.filas[0].cantidad === "1" && same(L.foco, { i: 0, campo: "cant" }) && !L.drop.abierto;
out.trailingEmptyLineAppears = L.filas.length === 2 && !L.filas[1].pid;
L.setCantidad(0, "12a");
out.quantityOnlyDigits = L.filas[0].cantidad === "12";
out.enterOnQuantityGoesToNewLine = L.tecla("Enter") === true && same(L.foco, { i: 1, campo: "prod" });
L.escribir(1, "galle");
L.tecla("Tab");
out.tabPicksToo = L.filas[1].pid === 2 && same(L.foco, { i: 1, campo: "cant" });
L.tecla("Enter");
out.secondLineDone = same(L.renglones(), [{ id: 1, cantidad: 12 }, { id: 2, cantidad: 1 }]) &&
  same(L.foco, { i: 2, campo: "prod" });
out.enterOnEmptyLineGoesToExit = L.tecla("Enter") === "salida";
// the same product again: one line, the cursor in its quantity
L.escribir(2, "gaseosa");
L.tecla("Enter");
out.sameProductIsOneLine = L.renglones().length === 2 && same(L.foco, { i: 0, campo: "cant" }) &&
  L.filas.filter((f) => f.pid === 1).length === 1;
// arrows between quantities
L.enfocar(0, "cant");
L.tecla("ArrowDown");
out.arrowDownBetweenQuantities = same(L.foco, { i: 1, campo: "cant" });
// a line emptied by hand, then ⌫: removed
L.escribir(1, "");
const vacia = !L.filas[1].pid;
out.backspaceRemovesEmptyLine = vacia && L.tecla("Backspace") === true &&
  same(L.renglones(), [{ id: 1, cantidad: 12 }]) && L.filas.length === 2 &&
  same(L.foco, { i: 0, campo: "cant" });
out.deleteOnFilledLineDoesNothing = (() => { L.enfocar(0, "prod"); return L.tecla("Delete") === false && L.renglones().length === 1; })();
// Escape closes the list and gives back the product that was there
L.filas[0]._antes = 1;
L.escribir(0, "agu");
const conLista = L.drop.abierto;
L.tecla("Escape");
out.escapeClosesAndRestores = conLista && !L.drop.abierto && L.filas[0].pid === 1;
// text that matches nothing stays (never a wrong product)
L.enfocar(1, "prod");
L.escribir(1, "zzzz");
out.noMatchStays = L.tecla("Enter") === true && L.filas[1].pid === null && L.sueltas() === 1;
L.escribir(1, "");

// totals and «a presupuestar»
const T = new P.Lineas(productos);
T.cargar([{ id: 1, cantidad: 2 }, { id: 4, cantidad: 3 }, { id: 99, cantidad: 1 }]);
const t = T.totales();
out.totalsSkipUnpriced = t.total === 18000 && t.presupuestar === 1 && t.productos === 2 && t.unidades === 5;
out.loadSkipsUnknown = T.renglones().length === 2 && T.filas.length === 3;
out.otherCustomerCatalogDropsLines = same(T.setCatalogo(productos.filter((p) => p.id !== 4)),
  ["Cigarrillos Rubios"]) && T.renglones().length === 1;
out.quantityBounds = P.cantidadValida("0") === 0 && P.cantidadValida("abc") === 0 &&
  P.cantidadValida("3") === 3 && P.cantidadValida("99999") === 10000;

// shortcuts
const k = (o) => P.atajo(Object.assign({ key: "", code: "", altKey: false, ctrlKey: false, metaKey: false }, o));
out.shortcutsExits = k({ altKey: true, code: "Digit1", key: "1" }) === "salida:1" &&
  k({ altKey: true, code: "Digit4", key: "4" }) === "salida:4" &&
  k({ altKey: true, code: "Numpad2", key: "2" }) === "salida:2" &&
  k({ altKey: true, code: "Digit5", key: "5" }) === null && k({ code: "Digit1", key: "1" }) === null;
out.shortcutsLoad = k({ ctrlKey: true, key: "Enter", code: "Enter" }) === "cargar" &&
  k({ metaKey: true, key: "Enter", code: "Enter" }) === "cargar" && k({ key: "Enter", code: "Enter" }) === null;
out.shortcutsLetters = k({ altKey: true, code: "KeyC", key: "ç" }) === "cliente" &&
  k({ altKey: true, code: "KeyP", key: "p" }) === "productos" &&
  k({ altKey: true, code: "KeyN", key: "n" }) === "nueva" &&
  k({ altKey: true, ctrlKey: true, code: "KeyN" }) === null;

// amounts
out.amounts = P.parseMonto("8.000") === 8000 && P.parseMonto("8000,50") === 8000.5 &&
  P.parseMonto("$ 1.234,5") === 1234.5 && P.parseMonto("") === 0 &&
  P.montoCampo(8000.5) === "8000,50" && P.montoCampo(10500) === "10500" && P.montoCampo(0) === "";

// where an error goes
out.errorsNextToTheirField =
  P.ubicarError("El cliente no tiene WhatsApp: poné el código de área y el número.") === "wa" &&
  P.ubicarError("Poné el motivo de la entrega fuera del sistema.") === "motivo" &&
  P.ubicarError("Poné el motivo de lo que devolvió (Gaseosa).") === "devolucion" &&
  P.ubicarError("Revisá estos archivos: nota.txt: No es una foto…") === "comprobante" &&
  P.ubicarError("Elegí el cliente.") === "cliente" &&
  P.ubicarError("Estos productos ya no están en el catálogo: X. Sacalos del carrito.") === "lineas" &&
  P.ubicarError("Algo raro") === "";

// layout per device
out.layoutChoice = P.elegirVista(null, true) === "planilla" && P.elegirVista(null, false) === "celular" &&
  P.elegirVista("celular", true) === "celular" && P.elegirVista("planilla", false) === "planilla";

// ------------------------------------------------------------ the phone page's switch

const xml = read("views/nueva_venta_app.xml");
const m = /<script t-if="v2" id="nv-vista">([\s\S]*?)<\/script>/.exec(xml);
const inline = m ? m[1].replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">") : "";
function vista(search, pc, init) {
  const localStorage = memoryStore(init);
  const location = { search, replaced: null, replace(u) { this.replaced = u; } };
  const w = { matchMedia: () => ({ matches: pc }) };
  new Function("location", "localStorage", "window", "matchMedia", inline)(
    location, localStorage, w, w.matchMedia);
  return { to: location.replaced, pref: localStorage.store["dn:nv:vista"] || null };
}
out.switchPcOpensPlanilla = !!inline && vista("", true).to === "/nueva-venta/planilla";
out.switchPhoneStays = vista("", false).to === null;
out.switchRemembersPhoneChoice = (() => {
  const a = vista("?celular=1", true);
  const b = vista("", true, { "dn:nv:vista": "celular" });
  return a.to === null && a.pref === "celular" && b.to === null;
})();
out.switchRemembersPlanillaChoice = vista("", false, { "dn:nv:vista": "planilla" }).to === "/nueva-venta/planilla";
out.switchNotWhenBackFromPicker = vista("?volver=1", true).to === null;

// ------------------------------------------------------------ the page on a stub DOM

function stubDom(search) {
  const els = {};
  const docHandlers = {};
  const doc = { activeElement: null };
  function el(id) {
    if (els[id]) { return els[id]; }
    const handlers = {};
    const e = {
      id, hidden: false, value: "", textContent: "", innerHTML: "", disabled: false, checked: false,
      tabIndex: 0, title: "", dataset: {}, style: {}, attrs: {}, handlers, elements: {}, offsetParent: {},
      classList: { add() {}, remove() {}, contains: () => false },
      addEventListener(t, f) { (handlers[t] = handlers[t] || []).push(f); },
      setAttribute(n, v) { this.attrs[n] = String(v); },
      getAttribute(n) { return n in this.attrs ? this.attrs[n] : null; },
      focus() { doc.activeElement = this; }, select() {}, click() { (handlers.click || []).forEach((f) => f({ target: this })); },
      reset() {}, scrollIntoView() {}, contains: () => false, closest: () => null,
      querySelector: () => el("_q" + Math.random()), querySelectorAll: () => [],
      getBoundingClientRect: () => ({ top: 0, bottom: 0, left: 0, width: 100 }),
      firstChild: { textContent: "" }, parentNode: null,
    };
    e.parentNode = e;
    e.elements = new Proxy({}, { get: (t, n) => (typeof n === "string" ? el("f-" + n) : undefined) });
    els[id] = e;
    return e;
  }
  const body = el("body");
  body.attrs = { "data-uid": "7", "data-wa-requerido": "0", "data-max-transf": "5" };
  const salidas = ["armar", "mostrador", "entregado", "presupuesto"].map((s, i) => {
    const b = el("op-" + s);
    b.dataset = { salida: s, atajo: String(i + 1) };
    return b;
  });
  Object.assign(doc, {
    body,
    getElementById: el,
    querySelector: (sel) => (sel === 'meta[name="csrf-token"]' ? { content: "tok" } : null),
    querySelectorAll: (sel) => (sel === "#pl-salidas [data-salida]" ? salidas : []),
    addEventListener(t, f) { (docHandlers[t] = docHandlers[t] || []).push(f); },
  });
  const localStorage = memoryStore();
  const location = { search: search || "", pathname: "/nueva-venta/planilla", href: "" };
  const win = { addEventListener() {}, confirm: () => true };
  const history = { state: null, replaceState() {} };
  const globals = { window: win, document: doc, localStorage, location, history, fetch: () => new Promise(() => {}),
    setTimeout, clearTimeout, crypto: { getRandomValues: (a) => a }, Uint8Array, FormData: class { has() { return true; } append() {} },
    AbortController: undefined };
  load("static/src/nueva_venta/planilla.js", globals);
  // the page reads window.* through the global names; mirror them
  return { el, doc, docHandlers, localStorage, salidas, win };
}
try {
  const dom = stubDom("?vista=planilla");
  out.planillaChoiceRemembered = dom.localStorage.store["dn:nv:vista"] === "planilla";
  dom.el("pl-celular").handlers.click[0]({});
  out.phoneChoiceRemembered = dom.localStorage.store["dn:nv:vista"] === "celular";
  out.startsInTheCustomerSearch = dom.doc.activeElement === dom.el("pl-q-cliente");
  const key = (o) => dom.docHandlers.keydown.forEach((f) => f(Object.assign(
    { key: "", code: "", altKey: false, ctrlKey: false, metaKey: false, preventDefault() { this.prevented = true; } }, o)));
  key({ altKey: true, code: "Digit2", key: "2" });
  out.altTwoChoosesMostrador = dom.el("pl-salida").value === "mostrador" &&
    dom.salidas[1].attrs["aria-checked"] === "true" && dom.salidas[0].attrs["aria-checked"] === "false";
  key({ altKey: true, code: "Digit4", key: "4" });
  out.altFourChoosesPresupuesto = dom.el("pl-salida").value === "presupuesto";
  key({ ctrlKey: true, key: "Enter", code: "Enter" });
  out.ctrlEnterWithoutCustomerSaysSo = dom.el("pl-err-cliente").hidden === false &&
    dom.el("pl-err-cliente").textContent === "Elegí el cliente.";
} catch (e) {
  out.stubDom = String(e && e.stack || e);
}

process.stdout.write(JSON.stringify(out));
