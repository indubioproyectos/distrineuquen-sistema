// 19.0.1.129.35: the phone side of «Ya lo armé» as a correction
// (static/src/armado/armado.js, confirma) and of «Nueva venta»
// (static/src/nueva_venta/nueva_venta.js: the draft kept on the phone,
// «Repetir» and the WhatsApp question). Runs each against a small fake page
// and prints the checks as JSON. Used by tests/test_v129_35_venta.py.
//   node venta_v129_35.js <module root>
"use strict";
const fs = require("fs");
const path = require("path");

const root = process.argv[2];
const read = (p) => fs.readFileSync(path.join(root, p), "utf8");
const tick = () => new Promise((r) => setImmediate(r));

function block(src, start, end) {
  const i = src.indexOf(start);
  const j = src.indexOf(end, i);
  if (i < 0 || j < 0) throw new Error("block not found: " + start);
  return src.slice(i, j + end.length);
}

// ------------------------------------------------------------ «Ya lo armé»

async function arm() {
  const code = block(read("static/src/armado/armado.js"), "function confirma(DN) {", "\n  }\n");
  let submitted = false;
  let asked = "";
  let answer = false;
  const campo = { value: "" };
  const form = {
    attrs: { "data-arm-confirma": "entregado",
             "data-arm-texto": "S00042 ya se ENTREGÓ al cliente: ¿Es así?" },
    getAttribute(n) { return this.attrs[n]; },
    querySelector() { return campo; },
    checkValidity() { return true; },
    reportValidity() {},
    submit() { submitted = true; },
    addEventListener(t, fn) { this.handler = fn; },
  };
  const DN = { $$: () => [form], confirm: (text) => { asked = text; return Promise.resolve(answer); } };
  new Function("DN", code + "\nconfirma(DN);")(DN);
  const ev = () => ({ prevented: false, preventDefault() { this.prevented = true; } });
  const first = ev();
  form.handler(first);
  await tick();
  const cancelKeeps = !submitted && campo.value === "";
  answer = true;
  form.handler(ev());
  await tick();
  const okSubmits = submitted;
  const third = ev();
  form.handler(third);
  return { firstPrevented: first.prevented, askedText: asked.indexOf("S00042") !== -1,
           cancelKeeps, okSubmits, confirmo: campo.value, secondPasses: !third.prevented };
}

// ------------------------------------------------------------ «Nueva venta»

async function nv() {
  const els = {};
  function makeEl(id) {
    return {
      id, value: "", hidden: false, checked: false, disabled: false, required: false,
      textContent: "", innerHTML: "", href: "", max: "", dataset: {}, listeners: {},
      addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); },
      fire(t, ev) { (this.listeners[t] || []).forEach((f) => f(ev || { target: this, type: t })); },
      querySelector(sel) { return el(id + ">" + sel); },
      focus() {}, scrollIntoView() {},
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
  const radios = ["armar", "mostrador", "entregado", "presupuesto"].map((v) => {
    const r = el("radio-" + v);
    r.value = v;
    return r;
  });
  const store = {};
  const localStorage = {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
  };
  const KEY = "dn:nv:borrador:7";
  store[KEY] = JSON.stringify({ t: Date.now(), cliente: { id: 5, nombre: "Almacén Test" },
    carrito: { 11: 4 }, salida: "armar", campos: { contacto: "Pedro" }, encomienda: false,
    sin_wa: false });
  const posted = [];
  const DN = {
    h: (v) => String(v == null ? "" : v),
    $: (sel, ctx) => {
      if (sel === "[data-nv-form]") return form;
      if (sel === "input[name=salida]:checked") return radios.find((r) => r.checked) || null;
      const m = /input\[name=salida\]\[value="(\w+)"\]/.exec(sel);
      if (m) return radios.find((r) => r.value === m[1]) || null;
      return null;
    },
    $$: (sel) => (sel === "input[name=salida]" ? radios : []),
    api(url, opts) {
      if (url.indexOf("/pedido/clientes") === 0) return Promise.resolve([]);
      if (url.indexOf("/nueva-venta/catalogo") === 0) {
        return Promise.resolve({
          ok: true,
          cliente: { id: 5, nombre: "Almacén Test", whatsapp: "", domicilio: "Mitre 1", lista: "",
                     sin_whatsapp: true, wa_area: "", wa_numero: "", cuenta: "" },
          productos: [{ id: 11, nombre: "Gaseosa", seccion: "", unidad: "u", contiene: "", precio: 100,
                        precio_texto: "100" },
                      { id: 12, nombre: "Galletitas", seccion: "", unidad: "u", contiene: "", precio: 50,
                        precio_texto: "50" }],
          anteriores: [{ nombre: "S00010", fecha: "20/11/2026", total: "$ 400", fuera: 0,
                         renglones: [{ id: 11, cantidad: 1 }, { id: 12, cantidad: 6 }] }],
        });
      }
      if (url === "/nueva-venta/crear") {
        posted.push(opts);
        return Promise.resolve({ ok: true, url: "/nueva-venta/hecho/9" });
      }
      return Promise.reject(new Error("unexpected " + url));
    },
    toast() {}, fail() {},
    sheet: () => ({ open() {}, close() {} }),
  };
  let assigned = "";
  const window = { DN, location: { assign: (u) => { assigned = u; } } };
  const document = {
    getElementById: el,
    body: { getAttribute: (n) => ({ "data-uid": "7", "data-wa-requerido": "0" })[n] || null },
  };
  class FormData { constructor(f) { this.form = f; } }
  const code = read("static/src/nueva_venta/nueva_venta.js");
  new Function("window", "document", "localStorage", "FormData", "navigator", code)(
    window, document, localStorage, FormData, {});
  await tick();
  const out = {};
  out.draftOffered = !el("nv-borrador").hidden &&
    el("nv-borrador-txt").textContent.indexOf("Almacén Test") !== -1;
  el("nv-borrador-seguir").fire("click");
  await tick(); await tick();
  out.restoredCart = JSON.parse(el("nv-renglones").value);
  out.restoredField = el("nv-contacto").value;
  out.waShown = !el("nv-wa").hidden;
  // the WhatsApp question unanswered: nothing is posted
  const ev = { preventDefault() {} };
  form.fire("submit", ev);
  await tick();
  out.waError = el("nv-error>span").textContent;
  out.postedWithoutWa = posted.length > 0;
  // «Repetir» replaces the cart
  el("nv-anteriores").fire("click", { target: { closest: () => ({ dataset: { rep: "0" } }) } });
  out.repeated = JSON.parse(el("nv-renglones").value);
  const saved = JSON.parse(store[KEY] || "null");
  out.draftSaved = !!saved && saved.carrito[12] === 6 && saved.salida === "armar";
  // answered: it goes and the draft is gone
  el("nv-wa-area").value = "299";
  el("nv-wa-numero").value = "4222444";
  form.fire("submit", ev);
  await tick(); await tick();
  out.draftClearedAfterSale = posted.length === 1 && assigned === "/nueva-venta/hecho/9" &&
    !(KEY in store);
  return out;
}

(async () => {
  const result = { arm: await arm(), nv: await nv() };
  process.stdout.write(JSON.stringify(result));
})().catch((e) => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
