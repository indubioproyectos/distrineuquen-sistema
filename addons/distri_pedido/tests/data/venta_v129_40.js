// 19.0.1.129.40: the phone side of «Nueva venta más rápida» and the two
// pure client-side fixes, against small fake pages. Prints the checks as
// JSON. Used by tests/test_v129_40_venta_rapida.py.
//   node venta_v129_40.js <module root>
// - static/src/kit/teclado.js: the keyboard closes after Enter / a choice;
// - static/src/nueva_venta/nueva_venta.js: a found product goes to the cart
//   once with the cursor in its quantity, −/+ on cart lines, the WhatsApp
//   not required, the draft taken up without asking when back (?volver=1);
// - static/src/nueva_venta/nueva_venta_productos.js: the picker round trip;
// - static/src/js/pedido_borrador.js: /pedido's cart kept on the device.
"use strict";
const fs = require("fs");
const path = require("path");

const root = process.argv[2];
const read = (p) => fs.readFileSync(path.join(root, p), "utf8");
const tick = () => new Promise((r) => setImmediate(r));
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

// ------------------------------------------------------------ teclado.js

function teclado() {
  const window = {};
  load("static/src/kit/teclado.js", { window, document: undefined });
  const T = window.DNTeclado;
  const doc = { activeElement: null, getElementById: () => null };
  function input(type, attrs) {
    const el = {
      tagName: "INPUT", type, attrs: Object.assign({}, attrs || {}), blurred: false, focused: false,
      getAttribute(n) { return n in this.attrs ? this.attrs[n] : null; },
      setAttribute(n, v) { this.attrs[n] = v; },
      hasAttribute(n) { return n in this.attrs; },
      closest(sel) { return sel.indexOf("data-dn-teclado") !== -1 && this.attrs["data-dn-teclado"] === "queda" ? this : (sel.indexOf("input") !== -1 ? this : null); },
      blur() { this.blurred = true; if (doc.activeElement === this) { doc.activeElement = null; } },
      focus() { this.focused = true; doc.activeElement = this; },
      disabled: false, readOnly: false, hidden: false, offsetParent: {}, getClientRects: () => [1],
    };
    return el;
  }
  const out = {};
  const search = input("search");
  doc.activeElement = search;
  out.enterBlurs = T.onKeyup({ key: "Enter", target: search }, doc) && search.blurred;
  const moved = input("text");
  const other = input("text");
  doc.activeElement = other;  // the page already moved the focus (caja: Enter = next)
  out.movedLeftAlone = !T.onKeyup({ key: "Enter", target: moved }, doc) && !moved.blurred;
  const ta = { tagName: "TEXTAREA", type: "textarea", blur() { this.blurred = true; } };
  doc.activeElement = ta;
  out.textareaKeeps = !T.onKeyup({ key: "Enter", target: ta }, doc) && !ta.blurred;
  const keep = input("search", { "data-dn-teclado": "queda" });
  doc.activeElement = keep;
  out.optOutKeeps = !T.onKeyup({ key: "Enter", target: keep }, doc) && !keep.blurred;
  const letter = input("search");
  doc.activeElement = letter;
  out.otherKeysKeep = !T.onKeyup({ key: "a", target: letter }, doc) && !letter.blurred;
  out.composingKeeps = !T.onKeyup({ key: "Enter", isComposing: true, target: letter }, doc);
  // enterkeyhint="next" goes to the next field of its form
  const a = input("text", { enterkeyhint: "next" });
  const b = input("text");
  a.form = b.form = { querySelectorAll: () => [a, b] };
  doc.activeElement = a;
  T.onKeyup({ key: "Enter", target: a }, doc);
  out.nextMoves = b.focused && !a.blurred;
  // a tap on a suggestion button closes it; on another field or label it does not
  const q = input("search");
  doc.activeElement = q;
  const btn = { tagName: "BUTTON", closest: () => null };
  out.tapBlurs = T.onTap({ target: btn }, doc) && q.blurred;
  const q2 = input("search");
  doc.activeElement = q2;
  const lbl = { tagName: "LABEL", closest: (sel) => (sel.indexOf("label") !== -1 ? lbl : null) };
  out.tapOnLabelKeeps = !T.onTap({ target: lbl }, doc) && !q2.blurred;
  // a <select> answered
  const sel = { tagName: "SELECT", blur() { this.blurred = true; }, closest: () => null };
  out.selectBlurs = T.onChoice({ type: "change", target: sel }, doc) && sel.blurred;
  // a datalist pick (input without inputType) vs typing
  const dl = { querySelectorAll: () => [{ value: "Zapala" }, { value: "Cutral Có" }] };
  const loc = input("text", { list: "locs" });
  loc.value = "Zapala";
  const doc2 = { activeElement: loc, getElementById: (id) => (id === "locs" ? dl : null) };
  out.typingKeeps = !T.onChoice({ type: "input", inputType: "insertText", target: loc }, doc2);
  out.datalistBlurs = T.onChoice({ type: "input", target: loc }, doc2) && loc.blurred;
  // enterkeyhint where missing
  const s1 = input("search");
  const n1 = input("text", { inputmode: "numeric" });
  const t1 = input("text");
  const own = input("search", { enterkeyhint: "go" });
  T.enhance({ querySelectorAll: () => [s1, n1, t1] });
  out.hints = [s1.getAttribute("enterkeyhint"), n1.getAttribute("enterkeyhint"), t1.getAttribute("enterkeyhint"),
               own.getAttribute("enterkeyhint")];
  return out;
}

// ------------------------------------------------------------ nueva_venta.js

async function nv(opts) {
  const els = {};
  const focused = [];
  function makeEl(id) {
    return {
      id, value: "", hidden: false, checked: false, disabled: false, required: false,
      textContent: "", innerHTML: "", href: "", max: "", dataset: {}, listeners: {},
      addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); },
      fire(t, ev) { (this.listeners[t] || []).forEach((f) => f(ev || { target: this, type: t })); },
      querySelector(sel) { return el(id + ">" + sel); },
      focus() { focused.push(id); }, select() { this.selected = true; }, scrollIntoView() { this.scrolled = true; },
    };
  }
  function el(id) { return els[id] || (els[id] = makeEl(id)); }
  const names = {
    entrega: "nv-entrega", telefono: "nv-telefono", domicilio: "nv-domicilio",
    comentario: "nv-comentario", nota_deposito: "nv-nota", fecha: "nv-fecha", motivo: "nv-motivo",
    contacto: "nv-contacto", wa_area: "nv-wa-area", wa_numero: "nv-wa-numero",
    enc_nombre: "nv-enc-nombre", enc_dni: "nv-enc-dni", enc_localidad: "nv-enc-localidad",
    lat: "nv-lat", lng: "nv-lng", precision: "nv-precision",
  };
  const form = el("nv-form");
  form.elements = {};
  Object.keys(names).forEach((n) => { form.elements[n] = el(names[n]); });
  form.checkValidity = () => true;
  form.reportValidity = () => {};
  const radios = ["armar", "presupuesto"].map((v) => { const r = el("radio-" + v); r.value = v; return r; });
  const KEY = "dn:nv:borrador:7";
  const local = memoryStore(opts.draft ? { [KEY]: JSON.stringify(opts.draft) } : {});
  const session = memoryStore(opts.session || {});
  const posted = [];
  const DN = {
    h: (v) => String(v == null ? "" : v),
    $: (sel) => {
      if (sel === "[data-nv-form]") return form;
      if (sel === "input[name=salida]:checked") return radios.find((r) => r.checked) || null;
      const m = /input\[name=salida\]\[value="(\w+)"\]/.exec(sel);
      if (m) return radios.find((r) => r.value === m[1]) || null;
      const q = /#nv-carrito \.nv-prod\[data-id="(\d+)"\] input/.exec(sel);
      if (q) return el("qty-" + q[1]);
      return null;
    },
    $$: (sel) => (sel === "input[name=salida]" ? radios : []),
    api(url, o) {
      if (url.indexOf("/pedido/clientes") === 0) return Promise.resolve([]);
      if (url.indexOf("/nueva-venta/catalogo") === 0) {
        return Promise.resolve({
          ok: true,
          cliente: { id: 5, nombre: "Almacén Test", whatsapp: "", domicilio: "", lista: "Minorista",
                     sin_whatsapp: true, wa_area: "", wa_numero: "", cuenta: "", calle: "", ciudad: "",
                     ficha: "/clientes/5?desde=/nueva-venta" },
          productos: [{ id: 11, nombre: "Gaseosa", seccion: "", unidad: "u", contiene: "", precio: 100,
                        precio_texto: "100" },
                      { id: 12, nombre: "Galletitas", seccion: "", unidad: "u", contiene: "", precio: 50,
                        precio_texto: "50" }],
          anteriores: [],
        });
      }
      if (url === "/nueva-venta/crear") { posted.push(o); return Promise.resolve({ ok: true, url: "/nueva-venta/hecho/9" }); }
      return Promise.reject(new Error("unexpected " + url));
    },
    toast() {}, fail() {},
    sheet: () => ({ open() {}, close() {} }),
  };
  let assigned = "";
  const window = { DN, location: { search: opts.search || "", pathname: "/nueva-venta", assign: (u) => { assigned = u; } } };
  const history = { state: null, replaceState(s, t, u) { this.replaced = u; } };
  window.history = history;
  const document = {
    getElementById: el,
    body: { getAttribute: (n) => ({ "data-uid": "7", "data-wa-requerido": opts.requerido ? "1" : "0" })[n] || null },
  };
  class FormData { constructor(f) { this.form = f; } }
  load("static/src/nueva_venta/nueva_venta.js", {
    window, document, localStorage: local, sessionStorage: session, FormData, navigator: {}, history });
  await tick(); await tick(); await tick();
  return { el, focused, posted, local, session, KEY, form, radios, history, get assigned() { return assigned; } };
}

function clickOn(box, id, what) {
  const row = { dataset: { id: String(id) } };
  box.fire("click", { type: "click", target: { closest: (sel) => {
    if (sel === ".nv-prod") return row;
    if (sel === "button[data-d]") return what.d ? { dataset: { d: String(what.d) } } : null;
    if (sel === "[data-elige]") return what.elige ? {} : null;
    return null;
  } } });
}

async function nvCart() {
  const out = {};
  // back from the picker (?volver=1): the draft goes on without asking
  const q = await nv({ draft: { t: Date.now(), cliente: { id: 5, nombre: "Almacén Test" }, carrito: {},
                                salida: "", campos: {} }, search: "?volver=1" });
  out.resumedWithoutAsking = q.el("nv-borrador-txt").textContent === "" && q.el("nv-cliente-id").value === 5;
  out.urlCleaned = q.history.replaced === "/nueva-venta";
  out.pickerLink = q.el("nv-elegir").href;
  out.fichaShown = !q.el("nv-ficha").hidden && q.el("nv-ficha").href === "/clientes/5?desde=/nueva-venta";
  // tap a found product: one line, cursor in its quantity
  q.el("nv-q-prod").value = "gas";
  clickOn(q.el("nv-productos"), 11, { elige: true });
  out.firstTap = JSON.parse(q.el("nv-renglones").value);
  out.focusedQty = q.focused.indexOf("qty-11") !== -1 && q.el("qty-11").selected === true;
  out.searchCleared = q.el("nv-q-prod").value === "";
  // the same product again: still one line (its quantity is where to type)
  clickOn(q.el("nv-productos"), 11, { elige: true });
  out.secondTap = JSON.parse(q.el("nv-renglones").value);
  // −/+ on the cart line
  clickOn(q.el("nv-carrito"), 11, { d: 1 });
  clickOn(q.el("nv-carrito"), 11, { d: 1 });
  clickOn(q.el("nv-carrito"), 11, { d: -1 });
  out.afterSteppers = JSON.parse(q.el("nv-renglones").value);
  clickOn(q.el("nv-carrito"), 11, { d: -1 });
  clickOn(q.el("nv-carrito"), 11, { d: -1 });
  out.removedAtZero = JSON.parse(q.el("nv-renglones").value);
  clickOn(q.el("nv-productos"), 12, { d: 1 });
  // the WhatsApp question: only at an exit that confirms, and not required
  out.waHiddenBeforeExit = q.el("nv-wa").hidden === true;
  q.radios[0].checked = true;
  q.radios[0].fire("change");
  out.waShownAtArmar = q.el("nv-wa").hidden === false;
  q.form.fire("submit", { preventDefault() {} });
  await tick(); await tick();
  out.postedWithoutWa = q.posted.length === 1 && q.assigned === "/nueva-venta/hecho/9" && !(q.KEY in q.local.store);
  // required (production): it stops the sale
  const r = await nv({ requerido: true, draft: { t: Date.now(), cliente: { id: 5, nombre: "Almacén Test" },
                                                 carrito: { 11: 2 }, salida: "armar", campos: {} },
                       session: { "dn:nv:volver": "1" } });
  out.resumedFromSessionFlag = r.el("nv-cliente-id").value === 5 && !("dn:nv:volver" in r.session.store);
  r.form.fire("submit", { preventDefault() {} });
  await tick();
  out.requiredStops = r.posted.length === 0 && r.el("nv-error>span").textContent.indexOf("WhatsApp") !== -1;
  // a draft with paso «salida» (back from the picker) shows the exit step
  const s = await nv({ draft: { t: Date.now(), cliente: { id: 5, nombre: "Almacén Test" }, carrito: { 12: 3 },
                                salida: "", campos: {}, paso: "salida" }, search: "?volver=1" });
  out.backAtExitStep = s.el("nv-paso-salida").scrolled === true;
  out.cartFromPicker = JSON.parse(s.el("nv-renglones").value);
  // without ?volver the draft is offered as in 129.35
  const t = await nv({ draft: { t: Date.now(), cliente: { id: 5, nombre: "Almacén Test" }, carrito: { 12: 3 },
                                salida: "", campos: {} } });
  out.offeredWithoutVolver = t.el("nv-borrador-txt").textContent.indexOf("Almacén Test") !== -1 &&
    t.el("nv-cliente-id").value === "";
  // leaving for the picker keeps the draft and marks the way back
  s.el("nv-elegir").fire("click");
  out.leavingMarks = s.session.store["dn:nv:volver"] === "1" && JSON.parse(s.local.store[s.KEY]).carrito[12] === 3;
  return out;
}

// ------------------------------------------------------------ the picker

function fila(id, qty, may) {
  const inp = { value: qty ? String(qty) : "" };
  return { attrs: { "data-id": String(id), "data-may": may ? "1" : null },
           getAttribute(n) { return this.attrs[n] || null; }, querySelector: () => inp, inp };
}

function picker() {
  const window = {};
  load("static/src/nueva_venta/nueva_venta_productos.js", { window, document: undefined });
  const P = window.DNNvProductos;
  const rows = [fila(11), fila(12), fila(13)];
  const doc = { querySelectorAll: () => rows };
  const now = Date.now();
  const draft = { t: now, cliente: { id: 5, nombre: "Almacén Test" }, carrito: { 11: 4, 99: 2 }, salida: "armar",
                  campos: { contacto: "Pedro" } };
  const local = memoryStore({ "dn:nv:borrador:7": JSON.stringify(draft) });
  const b = P.leer(local, "dn:nv:borrador:7", now);
  P.poner(doc, b.carrito);
  const out = {};
  out.filled = rows.map((r) => r.inp.value);
  rows[1].inp.value = "6";
  rows[0].inp.value = "5";
  const back = P.confirmar(b, { id: 5, nombre: "Almacén Test" }, P.carritoDe(doc), now + 1);
  out.cart = back.carrito;
  out.keptAnswers = back.salida === "armar" && back.campos.contacto === "Pedro" && back.paso === "salida";
  const other = P.confirmar(b, { id: 8, nombre: "Otro" }, { 12: 1 }, now);
  out.otherCustomerFresh = other.cliente.id === 8 && other.salida === "" && !other.campos.contacto;
  const old = memoryStore({ k: JSON.stringify(Object.assign({}, draft, { t: now - 8 * 864e5 })) });
  out.expired = P.leer(old, "k", now) === null;
  // a product drawn twice adds up
  const twice = { querySelectorAll: () => [fila(11, 2), fila(11, 3)] };
  out.twiceAdds = P.carritoDe(twice);
  return out;
}

// ------------------------------------------------------------ /pedido draft

function pedidoBorrador() {
  const window = {};
  load("static/src/js/pedido_borrador.js", { window, document: undefined });
  const B = window.DNPedidoBorrador;
  const rows = [fila(11, 2), fila(12, 0), fila(13, 1, true), fila(11, 0)];
  const elements = { nombre: { value: "Kiosco Ana" }, telefono: { value: "2995551234" },
                     domicilio: { value: "" }, entrega: { value: "retira", type: "hidden" },
                     encomienda: { checked: false }, clave: { value: "secreta", type: "password" } };
  const form = { elements, querySelectorAll: () => rows,
                 getAttribute: (n) => (n === "data-borrador" ? "visitante:1" : null) };
  const local = memoryStore();
  const now = Date.now();
  const key = B.clave(form);
  B.guardar(local, key, form, now);
  const saved = JSON.parse(local.store[key]);
  const out = { key, saved: saved.cantidades, savedFields: saved.campos };
  // the page drawn again: product 12 is gone, a new 14 is there
  const rows2 = [fila(11), fila(13, 0, true), fila(14), fila(11)];
  const els2 = { nombre: { value: "" }, telefono: { value: "" }, domicilio: { value: "" },
                 entrega: { value: "domicilio", type: "hidden" } };
  const form2 = { elements: els2, querySelectorAll: () => rows2, getAttribute: () => "visitante:1" };
  const b = B.leer(local, key, now + 1000);
  out.restoredCount = B.restaurar(form2, b);
  out.restoredRows = rows2.map((r) => r.inp.value);
  out.restoredFields = [els2.nombre.value, els2.telefono.value, els2.entrega.value];
  out.expired = B.leer(local, key, now + 4 * 864e5) === null && !(key in local.store);
  // an empty cart removes it
  B.guardar(local, key, { elements: {}, querySelectorAll: () => [fila(11)], getAttribute: () => "visitante:1" }, now);
  out.emptyRemoves = !(key in local.store);
  out.userKey = B.clave({ getAttribute: () => "7:3" });
  return out;
}

(async () => {
  const result = { teclado: teclado(), nv: await nvCart(), picker: picker(), borrador: pedidoBorrador() };
  process.stdout.write(JSON.stringify(result));
})().catch((e) => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
