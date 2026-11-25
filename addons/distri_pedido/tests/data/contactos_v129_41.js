// 19.0.1.129.41: the typing side of «Contactos» (static/src/kit/contactos.js)
// against fake timers and a fake fetch. Prints the checks as JSON. Used by
// tests/test_v129_41_contactos.py.
//   node contactos_v129_41.js <module root>
// - the query of a search state;
// - typing waits for the pause (debounce), a filter searches at once;
// - a late answer of an older search is never drawn;
// - «Ver más» asks the next page and appends;
// - a row is built with textContent (data never parsed as HTML).
"use strict";
const fs = require("fs");
const path = require("path");

const root = process.argv[2];
const window = {};
new Function("window", fs.readFileSync(path.join(root, "static/src/kit/contactos.js"), "utf8"))(window);
const C = window.DNContactos;
const tick = () => new Promise((r) => setImmediate(r));

function fakeTimers() {
  let now = 0;
  const timers = [];
  return {
    setTimeout(fn, ms) { const t = {fn, at: now + ms, id: timers.length + 1}; timers.push(t); return t.id; },
    clearTimeout(id) { const t = timers.find((x) => x.id === id); if (t) { t.fn = null; } },
    advance(ms) {
      now += ms;
      timers.filter((t) => t.fn && t.at <= now).forEach((t) => { const fn = t.fn; t.fn = null; fn(); });
    },
  };
}

async function main() {
  const out = {};
  out.query = C.query({q: "  kiosco   ana ", localidad: "Plottier", tipo: "clientes", mios: true,
                       sin_wa: true, deuda: false});
  out.queryPage = C.query({q: "x", tipo: "proveedores"}, 2);

  // typing: nothing until the pause, then one request for the last text
  const timers = fakeTimers();
  const fetched = [];
  const answers = {};
  const drawn = [];
  const states = [];
  let appended = false;
  const search = C.createSearch({
    base: "/contactos/buscar",
    setTimeout: timers.setTimeout,
    clearTimeout: timers.clearTimeout,
    fetch: (url) => { fetched.push(url); return new Promise((resolve) => { answers[url] = resolve; }); },
    render: (data, append) => { drawn.push(data.tag); if (append) { appended = true; } },
    onState: (key) => states.push(key),
  });
  const pending = [];
  ["k", "ki", "kio", "kios"].forEach((q) => { pending.push(search.set({q}, false)); timers.advance(100); });
  out.fetchedWhileTyping = fetched.length;
  timers.advance(C.DELAY);
  out.fetchedAfterPause = fetched.slice();
  // a filter goes at once; then the old typed answer arrives late
  const first = fetched[0];
  const filterRun = search.set({q: "kios", mios: true}, true);
  out.filterNow = fetched.length - 1;
  const second = fetched[fetched.length - 1];
  answers[second]({ok: true, tag: "nuevo", filas: [], total: 0, mas: false, page: 0});
  await filterRun;
  answers[first]({ok: true, tag: "viejo", filas: [], total: 3, mas: true, page: 0});
  await tick();
  await Promise.all(pending);
  out.drawn = drawn.slice();
  // back to the typed text only, then «Ver más»
  const back = search.set({q: "kios"}, true);
  answers[fetched[fetched.length - 1]]({ok: true, tag: "otra", filas: [], total: 40, mas: true, page: 0});
  await back;
  const more = search.more();
  out.moreUrl = fetched[fetched.length - 1];
  answers[out.moreUrl]({ok: true, tag: "pagina2", filas: [], total: 40, mas: false, page: 1});
  await more;
  out.appended = appended && search.page() === 1;
  // the same state again: no request
  const count = fetched.length;
  await search.set({q: "kios"}, true);
  out.sameStateSkipped = fetched.length === count;
  out.states = states;

  // a row built without innerHTML
  const doc = {
    createElement(tag) {
      return {
        tag, className: "", attrs: {}, children: [], _text: "", html: false,
        setAttribute(k, v) { this.attrs[k] = v; },
        appendChild(c) { this.children.push(c); return c; },
        set textContent(v) { this._text = v; },
        get textContent() { return this._text + this.children.map((c) => c.textContent).join("|"); },
        set innerHTML(v) { this.html = true; },
      };
    },
  };
  const row = C.buildRow(doc, {id: 5, nombre: "<img src=x onerror=1>", codigo: "C1", localidad: "Plottier",
                               vendedor: "Ana", whatsapp: "", saldo: "$ 10", deuda: true,
                               confirmar: true, archivado: false, url: "/clientes/5?desde=/contactos"});
  const anyHtml = (n) => n.html || n.children.some(anyHtml);
  out.row = {href: row.attrs.href, text: row.textContent, html: anyHtml(row)};
  out.total = [C.totalText(1), C.totalText(3)];
  process.stdout.write(JSON.stringify(out));
}

main().catch((e) => { console.error(e); process.exit(1); });
