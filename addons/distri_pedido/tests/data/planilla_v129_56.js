// 19.0.1.129.56: «Nueva venta» on the PC (static/src/nueva_venta/planilla.js),
// the counter sale closed on one screen. Prints the checks as JSON; used by
// tests/test_v129_56_mostrador.py.
//   node planilla_v129_56.js <module root>
// - the steps on top (where the sale stands) and their names;
// - the customer's balance chip;
// - the new shortcuts (Alt+G «Corregir la venta», Alt+R the remito);
// - on a stub DOM, a sale delivered on account: what is still owed, «Cobrar»
//   offered, the remito printed (Alt+R) or handed as a file to share;
// - on a stub DOM, with the server answered by a fake fetch: a counter sale
//   waiting for its delivery is restored, «Cuenta corriente» posts «no pagó»,
//   «Corregir la venta» asks first, posts to its endpoint and puts the
//   customer and the lines back to load a new sale (another `envio`).
"use strict";
const fs = require("fs");
const path = require("path");

const root = process.argv[2];
const read = (p) => fs.readFileSync(path.join(root, p), "utf8");
function load(file, globals) {
  const names = Object.keys(globals);
  new Function(...names, read(file))(...names.map((n) => globals[n]));
}
const out = {};
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// ------------------------------------------------------------ the pure core

const window = {};
load("static/src/nueva_venta/planilla.js", { window, document: undefined });
const P = window.DNPlanilla;

const entrega = { pedido: 10500, deuda: 0, bultos: 1, lineas: [] };
out.stepsEmpty = same(P.pasosDe(null), { carga: "ahora", cargada: "", entregada: "", cobrada: "" });
out.stepsCounterWaiting = same(
  P.pasosDe({ estado: "sale", facturada: true, residual: 10500, entrega, entregado: false }),
  { carga: "hecho", cargada: "hecho", entregada: "ahora", cobrada: "" });
out.stepsDeliveredAndPaid = same(
  P.pasosDe({ estado: "sale", facturada: true, residual: 0, entrega: null, entregado: true, cobrar: null }),
  { carga: "hecho", cargada: "hecho", entregada: "hecho", cobrada: "hecho" });
out.stepsDeliveredOwing = P.pasosDe(
  { estado: "sale", facturada: true, residual: 10500, entregado: true, cobrar: { debe: 10500 } }).cobrada === "ahora";
out.stepsTransferOnItsWay = P.pasosDe(
  { estado: "sale", facturada: true, residual: 10500, en_camino: 10500, entregado: true, cobrar: null }).cobrada === "no";
out.stepsToPacking = same(
  P.pasosDe({ estado: "sale", facturada: false, residual: 0, entrega: null, entregado: false }),
  { carga: "hecho", cargada: "hecho", entregada: "no", cobrada: "no" });
out.stepsCancelled = same(P.pasosDe({ estado: "cancel", facturada: true }),
  { carga: "hecho", cargada: "no", entregada: "no", cobrada: "no" });

out.namesByState = P.textoPasos(null).cargada === "Cargada" &&
  P.textoPasos({ estado: "sale", facturada: true }).cargada === "Facturada" &&
  P.textoPasos({ estado: "sale", facturada: false }).cargada === "Confirmada · va a Armado" &&
  P.textoPasos({ estado: "draft", facturada: false }).cargada === "Presupuesto guardado" &&
  P.textoPasos({ estado: "cancel", facturada: true }).cargada === "Anulada";
out.namesOnAccount = P.textoPasos(
  { estado: "sale", facturada: true, entregado: true, residual: 10500, en_camino: 0, cobrar: null }).cobrada === "En cuenta corriente" &&
  P.textoPasos(
    { estado: "sale", facturada: true, entregado: true, residual: 10500, en_camino: 10500, cobrar: null }).cobrada === "Transferencia a verificar" &&
  P.textoPasos({ estado: "sale", facturada: true, entregado: true, residual: 0, cobrar: null }).cobrada === "Cobrada";

out.balanceChip = P.chipSaldo({ saldo: null }) === null && P.chipSaldo(null) === null &&
  same(P.chipSaldo({ saldo: 0, saldo_texto: "$ 0" }), { texto: "Al día", clase: "dn-chip--ok" }) &&
  same(P.chipSaldo({ saldo: 25000, saldo_texto: "$ 25.000" }), { texto: "Debe $ 25.000", clase: "dn-chip--warn" }) &&
  same(P.chipSaldo({ saldo: -300, saldo_texto: "$ 300" }), { texto: "A favor $ 300", clase: "dn-chip--ok" });

const ev = (o) => Object.assign({ key: "", code: "", altKey: false, ctrlKey: false, metaKey: false }, o);
out.newShortcuts = P.atajo(ev({ altKey: true, code: "KeyG", key: "g" })) === "corregir" &&
  P.atajo(ev({ altKey: true, code: "KeyR", key: "r" })) === "remito" &&
  P.atajo(ev({ altKey: true, code: "KeyN", key: "n" })) === "nueva" &&
  P.atajo(ev({ code: "KeyG", key: "g" })) === null &&
  P.atajo(ev({ altKey: true, ctrlKey: true, code: "KeyG", key: "g" })) === null;

// ------------------------------------------------------------ the page on a stub DOM

const PRODUCTOS = [
  { id: 11, nombre: "Gaseosa Cola", seccion: "Bebidas", unidad: "Pack", contiene: 6, precio: 4000 },
  { id: 12, nombre: "Galletitas", seccion: "Almacén", unidad: "Unidad", contiene: 0, precio: 2500 },
];
const CLIENTE = { id: 5, nombre: "Almacén Stub", whatsapp: "2995001156", domicilio: "Mitre 1", lista: "General",
  sin_whatsapp: false, cuenta: "/cuentas#cuenta/5", ficha: "", saldo: 25000, saldo_texto: "$ 25.000" };
const VENTA = {
  ok: true, id: 77, nombre: "S00077", cliente: "Almacén Stub", cliente_id: 5, total: 10500, total_texto: "$ 10.500",
  titulo: "Venta en mostrador", texto: "Confirmada, armada y facturada.", estado: "sale", entregado: false,
  entrega: { pedido: 10500, deuda: 25000, bultos: 1, tiene_codigo: false, lineas: [] },
  cobrar: { debe: 10500, debe_texto: "$ 10.500", factura_id: 9, max: 5 }, pagos: [],
  documentos: [{ tipo: "Factura", nombre: "0001/2026/09000", url: "/facturas/9" }], enviar: "",
  facturada: true, residual: 10500, residual_texto: "$ 10.500", en_camino: 0, en_camino_texto: "$ 0",
  remito: { nombre: "R-00500", url: "/report/pdf/x/77", archivo: "Remito R-00500.pdf" },
  corregir: { pasos: ["Se emite una nota de crédito por $ 10.500.", "Vuelve al stock lo del remito R-00500."], motivo: "" },
};

function stubDom(draft, routes) {
  const els = {};
  const docHandlers = {};
  const calls = [];
  const doc = { activeElement: null };
  function el(id) {
    if (els[id]) { return els[id]; }
    const handlers = {};
    const e = {
      id, hidden: false, value: "", textContent: "", innerHTML: "", disabled: false, checked: false, className: "",
      tabIndex: 0, title: "", dataset: {}, style: {}, attrs: {}, handlers, elements: {}, offsetParent: {},
      classList: { add() {}, remove() {}, contains: () => false },
      addEventListener(t, f) { (handlers[t] = handlers[t] || []).push(f); },
      setAttribute(n, v) { this.attrs[n] = String(v); },
      removeAttribute(n) { delete this.attrs[n]; },
      getAttribute(n) { return n in this.attrs ? this.attrs[n] : null; },
      focus() { doc.activeElement = this; }, select() {},
      click() { (handlers.click || []).forEach((f) => f({ target: this })); },
      reset() {}, scrollIntoView() {}, contains: () => false, closest: () => null,
      appendChild() {}, remove() {},
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
  const pasos = ["carga", "cargada", "entregada", "cobrada"].map((k) => {
    const li = el("paso-" + k);
    li.attrs["data-paso"] = k;
    const t = el("paso-t-" + k);
    li.querySelector = () => (k === "cargada" || k === "cobrada" ? t : null);
    return li;
  });
  Object.assign(doc, {
    body,
    getElementById: el,
    createElement: (tag) => {
      const e = el("_new-" + tag + "-" + Math.random());
      if (tag === "iframe") { e.contentWindow = { focus() {}, print() { doc.printed = (doc.printed || 0) + 1; } }; }
      if (tag === "a") { e.click = () => { doc.downloads = (doc.downloads || []).concat(e.download); }; }
      return e;
    },
    querySelector: (sel) => (sel === 'meta[name="csrf-token"]' ? { content: "tok" } : null),
    querySelectorAll: (sel) => (sel === "#pl-salidas [data-salida]" ? salidas
      : sel === "#pl-pasos [data-paso]" ? pasos : []),
    addEventListener(t, f) { (docHandlers[t] = docHandlers[t] || []).push(f); },
  });
  const store = {};
  if (draft) { store["dn:nv:planilla:7"] = JSON.stringify(draft); }
  const localStorage = {
    store,
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
  };
  class FormData {
    constructor(form) { this.form = form || null; this.data = {}; }
    has(k) { return k in this.data; }
    append(k, v) { this.data[k] = v; }
    delete(k) { delete this.data[k]; }
  }
  const fetch = (url, opts) => {
    calls.push({ url, method: (opts && opts.method) || "GET", body: opts && opts.body });
    const key = Object.keys(routes).find((k) => url.indexOf(k) === 0);
    const data = key ? routes[key] : { ok: false, error: "404" };
    return Promise.resolve({
      status: key ? 200 : 404, ok: !!key, redirected: false, url,
      headers: { get: () => "application/json" }, json: () => Promise.resolve(JSON.parse(JSON.stringify(data))),
      blob: () => Promise.resolve(new Blob(["%PDF-1.4"], { type: "application/pdf" })),
    });
  };
  const location = { search: "", pathname: "/nueva-venta/planilla", href: "" };
  const win = { addEventListener() {}, confirm: () => true, open() {} };
  const globals = { window: win, document: doc, localStorage, location, history: { state: null, replaceState() {} },
    fetch, setTimeout, clearTimeout, crypto: { getRandomValues: (a) => a.fill(7) }, Uint8Array, FormData,
    AbortController: undefined, navigator: {}, URL: { createObjectURL: () => "blob:x" } };
  load("static/src/nueva_venta/planilla.js", globals);
  const key = (o) => docHandlers.keydown.forEach((f) => f(Object.assign(
    { key: "", code: "", altKey: false, ctrlKey: false, metaKey: false, preventDefault() {} }, o)));
  return { el, els, doc, calls, localStorage, salidas, pasos, key };
}
const tick = async (n) => { for (let i = 0; i < (n || 12); i++) { await new Promise((r) => setImmediate(r)); } };

(async () => {
  try {
    const routes = {
      "/nueva-venta/catalogo": { ok: true, cliente: CLIENTE, productos: PRODUCTOS, anteriores: [] },
      "/nueva-venta/planilla/venta/77": VENTA,
      "/nueva-venta/planilla/corregir/77": {
        ok: true, nombre: "S00077", texto: "S00077 se anuló con su nota de crédito.",
        renglones: [{ id: 11, cantidad: 2 }, { id: 12, cantidad: 1 }, { id: 99, cantidad: 4 }] },
      "/deposito/entrega/77": { ok: true, message: "S00077 entregado a Almacén Stub." },
    };
    const draft = { t: Date.now(), cliente: { id: 5, nombre: "Almacén Stub" },
      renglones: [{ id: 11, cantidad: 2 }, { id: 12, cantidad: 1 }], salida: "mostrador", campos: {},
      envio: "aaaa", encomienda: false, venta_id: 77 };
    const dom = stubDom(draft, routes);
    await tick();

    // a counter sale waiting for its delivery is restored on the same screen
    out.restoresTheLoadedSale = dom.el("pl-card-entrega").hidden === false &&
      dom.el("pl-card-salida").hidden === true && dom.el("pl-entrega-venta").textContent === "S00077";
    out.balanceShown = dom.el("pl-saldo").hidden === false && dom.el("pl-saldo").textContent === "Debe $ 25.000" &&
      dom.el("pl-saldo").className === "dn-chip dn-chip--warn";
    out.stepsShowTheDelivery = dom.pasos[1].className === "pl-paso--hecho" &&
      dom.pasos[2].className === "pl-paso--ahora" && dom.pasos[2].attrs["aria-current"] === "step" &&
      dom.el("paso-t-cargada").textContent === "Facturada";
    out.cashPrefilled = dom.el("pl-efectivo").value === "10500";
    out.correctionOffered = dom.el("pl-corregir").hidden === false && dom.el("pl-corregir-btn").disabled === false &&
      dom.el("pl-corregir-pregunta").hidden === true;
    out.remitoOffered = dom.el("pl-h-remito").hidden === false;

    // «Cuenta corriente»: nothing paid now, said on the button
    dom.el("pl-entrega-form").handlers.input[0]({ target: {} });
    out.payButtonByDefault = dom.el("pl-entregar-t").textContent === "Entregar y cobrar" &&
      dom.el("pl-cc-aviso").hidden === true;

    // Alt+G asks first, and only «Sí» posts
    dom.key({ altKey: true, code: "KeyG", key: "g" });
    out.correctionAsksFirst = dom.el("pl-corregir-pregunta").hidden === false &&
      dom.el("pl-corregir-nombre").textContent === "S00077" &&
      dom.el("pl-corregir-pasos").innerHTML.indexOf("nota de crédito") !== -1 &&
      !dom.calls.some((c) => c.url.indexOf("/corregir/") !== -1);
    dom.el("pl-corregir-no").click();
    out.correctionCanBeLeft = dom.el("pl-corregir-pregunta").hidden === true &&
      !dom.calls.some((c) => c.url.indexOf("/corregir/") !== -1);
    dom.key({ altKey: true, code: "KeyG", key: "g" });
    dom.el("pl-corregir-si").click();
    await tick();
    const posted = dom.calls.filter((c) => c.url === "/nueva-venta/planilla/corregir/77");
    out.correctionPostsOnce = posted.length === 1 && posted[0].method === "POST";

    // back to editing: same customer, the lines that are still sold, «Mostrador», a new `envio`
    out.backToEditing = dom.el("pl-card-salida").hidden === false && dom.el("pl-card-entrega").hidden === true &&
      dom.el("pl-card-hecho").hidden === true && dom.el("pl-corregir").hidden === true &&
      dom.el("pl-cambiar").hidden === false;
    out.linesAreBack = dom.el("pl-renglones").value === JSON.stringify([{ id: 11, cantidad: 2 }, { id: 12, cantidad: 1 }]);
    out.saysWhatHappened = dom.el("pl-corregida").hidden === false &&
      dom.el("pl-corregida").textContent.indexOf("S00077") !== -1;
    out.warnsAboutAProductGone = dom.el("pl-err-lineas").hidden === false &&
      dom.el("pl-err-lineas").textContent.indexOf("ya no está en el catálogo") !== -1;
    out.keepsTheCounterExit = dom.el("pl-salida").value === "mostrador" && dom.el("pl-cargar").disabled === false;
    out.stepsBackToTheStart = dom.pasos[0].className === "pl-paso--ahora" && dom.pasos[2].className === "";
    const saved = JSON.parse(dom.localStorage.store["dn:nv:planilla:7"]);
    out.draftForgetsTheCancelledSale = saved.venta_id == null && saved.envio === "" &&
      saved.renglones.length === 2 && saved.salida === "mostrador";

    // the changed sale is another one: Ctrl+Enter posts with another `envio`
    dom.key({ ctrlKey: true, key: "Enter", code: "Enter" });
    await tick();
    out.loadsANewSale = dom.calls.some((c) => c.url === "/nueva-venta/crear" && c.method === "POST") &&
      dom.el("pl-envio").value.length === 32 && dom.el("pl-envio").value !== "aaaa";
  } catch (e) {
    out.stubDom = String((e && e.stack) || e);
  }

  // a sale delivered on account, restored: what is owed, «Cobrar», the remito
  try {
    const entregada = Object.assign({}, VENTA, {
      entregado: true, entrega: null, corregir: null, a_cuenta: true, texto: "Entregada en el depósito, con su factura.",
      pagos: ["En la entrega no pagó: queda en su cuenta corriente"] });
    const dom = stubDom(
      { t: Date.now(), cliente: { id: 5, nombre: "Almacén Stub" }, renglones: [{ id: 11, cantidad: 2 }],
        salida: "mostrador", campos: {}, envio: "bbbb", encomienda: false, venta_id: 77 },
      { "/nueva-venta/catalogo": { ok: true, cliente: CLIENTE, productos: PRODUCTOS, anteriores: [] },
        "/nueva-venta/planilla/venta/77": entregada, "/report/pdf/x/77": {} });
    await tick();
    out.onAccountSaysWhatIsOwed = dom.el("pl-h-saldo").hidden === false &&
      dom.el("pl-h-saldo").textContent === "Queda en su cuenta corriente: $ 10.500." &&
      dom.el("pl-h-saldo").className === "pl-h-saldo pl-h-saldo--debe";
    out.onAccountOffersCobrar = dom.el("pl-card-cobrar").hidden === false && dom.el("pl-card-entrega").hidden === true &&
      dom.el("pl-corregir").hidden === true;
    out.onAccountDoesNotOfferAnAmount = dom.el("pl-c-efectivo").value === "" &&
      dom.doc.activeElement === dom.el("pl-nueva");
    out.onAccountSteps = dom.pasos[2].className === "pl-paso--hecho" && dom.pasos[3].className === "pl-paso--ahora";
    // Alt+G does nothing once delivered
    dom.key({ altKey: true, code: "KeyG", key: "g" });
    out.noCorrectionOnceDelivered = dom.el("pl-corregir-pregunta").hidden !== false &&
      !dom.calls.some((c) => c.url.indexOf("/corregir/") !== -1);
    // Alt+R prints the remito's PDF
    dom.key({ altKey: true, code: "KeyR", key: "r" });
    await tick();
    const frames = Object.keys(dom.els).filter((k) => k.indexOf("_new-iframe-") === 0).map((k) => dom.els[k]);
    out.remitoFetched = dom.calls.some((c) => c.url === "/report/pdf/x/77");
    frames.forEach((f) => f.onload && f.onload());
    await new Promise((r) => setTimeout(r, 350));
    out.remitoPrinted = frames.length === 1 && dom.doc.printed === 1;
    // «Compartir remito» without a share sheet: the file to drag into the chat
    dom.el("pl-compartir").click();
    await tick();
    out.remitoHandedAsAFile = same(dom.doc.downloads, ["Remito R-00500.pdf"]) &&
      dom.el("pl-h-remito-aviso").textContent.indexOf("Remito R-00500.pdf") !== -1;
  } catch (e) {
    out.stubDomDelivered = String((e && e.stack) || e);
  }
  process.stdout.write(JSON.stringify(out));
})();
