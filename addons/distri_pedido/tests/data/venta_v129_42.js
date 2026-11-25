// 19.0.1.129.42 (item 1): «Nueva venta»'s customer list against a small fake
// page, fake timers and a fake DN.api. Prints the checks as JSON. Used by
// tests/test_v129_42_venta_clientes.py.
//   node venta_v129_42.js <module root>
// - nothing is asked on open nor on «Cambiar»: a one-line hint instead;
// - one letter asks nothing; from the 2nd letter, one request after the pause;
// - a late answer of an older search is never drawn;
// - the cursor goes to the search on open (no draft).
"use strict";
const fs = require("fs");
const path = require("path");

const root = process.argv[2];
const read = (p) => fs.readFileSync(path.join(root, p), "utf8");
const tick = () => new Promise((r) => setImmediate(r));

function fakeTimers() {
  let now = 0;
  const timers = [];
  return {
    setTimeout(fn, ms) { const t = { fn, at: now + ms, id: timers.length + 1 }; timers.push(t); return t.id; },
    clearTimeout(id) { const t = timers.find((x) => x.id === id); if (t) { t.fn = null; } },
    advance(ms) {
      now += ms;
      timers.filter((t) => t.fn && t.at <= now).forEach((t) => { const fn = t.fn; t.fn = null; fn(); });
    },
  };
}

async function main() {
  const els = {};
  const focused = [];
  function makeEl(id) {
    return {
      id, value: "", hidden: false, checked: false, disabled: false, required: false,
      textContent: "", innerHTML: "", href: "", max: "", dataset: {}, listeners: {},
      addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); },
      fire(t, ev) { (this.listeners[t] || []).forEach((f) => f(ev || { target: this, type: t })); },
      querySelector(sel) { return el(id + ">" + sel); },
      focus() { focused.push(id); }, select() {}, scrollIntoView() {},
    };
  }
  function el(id) { return els[id] || (els[id] = makeEl(id)); }
  const form = el("nv-form");
  form.elements = {};
  form.checkValidity = () => true;
  const asked = [];
  const pending = {};
  const DN = {
    h: (v) => String(v == null ? "" : v),
    $: (sel) => (sel === "[data-nv-form]" ? form : null),
    $$: () => [],
    api(url) {
      if (url.indexOf("/pedido/clientes") === 0) {
        asked.push(url);
        return new Promise((resolve) => { pending[url] = resolve; });
      }
      return Promise.reject(new Error("unexpected " + url));
    },
    toast() {}, fail() {},
    sheet: () => ({ open() {}, close() {} }),
  };
  const timers = fakeTimers();
  const window = { DN, location: { search: "", pathname: "/nueva-venta", assign() {} } };
  const store = { getItem: () => null, setItem() {}, removeItem() {} };
  const document = {
    getElementById: el,
    body: { getAttribute: (n) => ({ "data-uid": "7", "data-wa-requerido": "0" })[n] || null },
  };
  const names = ["window", "document", "localStorage", "sessionStorage", "FormData", "navigator",
                 "history", "setTimeout", "clearTimeout"];
  const values = [window, document, store, store, class {}, {}, { replaceState() {} },
                  timers.setTimeout, timers.clearTimeout];
  new Function(...names, read("static/src/nueva_venta/nueva_venta.js"))(...values);
  await tick();
  const out = {};
  const NV = window.DNNuevaVenta;
  out.helper = { a: NV.debeBuscar("a"), ab: NV.debeBuscar("ab"), spaces: NV.debeBuscar("  a "),
                 min: NV.MIN_LETRAS };
  out.askedOnOpen = asked.length;
  out.hintOnOpen = el("nv-clientes").innerHTML;
  out.focusedOnOpen = focused.indexOf("nv-q-cliente") !== -1;
  // one letter: nothing asked
  const q = el("nv-q-cliente");
  q.value = "a";
  q.fire("input");
  timers.advance(300);
  out.askedOneLetter = asked.length;
  out.hintOneLetter = el("nv-clientes").innerHTML;
  // two letters: one request after the pause
  q.value = "al";
  q.fire("input");
  timers.advance(100);
  out.askedBeforePause = asked.length;
  timers.advance(300);
  out.askedAfterPause = asked.slice();
  // a newer search, then the older answer arrives late: not drawn
  q.value = "alm";
  q.fire("input");
  timers.advance(300);
  pending["/pedido/clientes?q=al"]([{ id: 1, nombre: "Viejo", ciudad: "", whatsapp: "" }]);
  await tick(); await tick();
  out.lateOldDrawn = el("nv-clientes").innerHTML.indexOf("Viejo") !== -1;
  pending["/pedido/clientes?q=alm"]([{ id: 2, nombre: "Almacén Nuevo", ciudad: "", whatsapp: "" }]);
  await tick(); await tick();
  out.newestDrawn = el("nv-clientes").innerHTML.indexOf("Almacén Nuevo") !== -1;
  // «Cambiar»: the search is emptied, the list folds, nothing asked
  const before = asked.length;
  el("nv-cambiar").fire("click");
  timers.advance(300);
  out.askedOnCambiar = asked.length - before;
  out.searchClearedOnCambiar = q.value === "";
  out.hintOnCambiar = el("nv-clientes").innerHTML;
  process.stdout.write(JSON.stringify(out));
}

main().catch((e) => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
