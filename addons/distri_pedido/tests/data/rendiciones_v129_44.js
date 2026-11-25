// 19.0.1.129.44: the «¿Seguro?» of static/src/kit/rendiciones_v2.js (the
// askFirst block) against fake forms. Prints the checks as JSON. Used by
// tests/test_v129_44_rendiciones.py.
//   node rendiciones_v129_44.js <module root>
// - a form whose confirmar is empty is stopped and asks with its own text;
// - «Cancelar» sends nothing, leaves confirmar empty and gives the buttons back;
// - «Sí» sets confirmar=1 and sends the form once;
// - a form already confirmed (or without confirmar) is not stopped.
"use strict";
const fs = require("fs");
const path = require("path");

const root = process.argv[2];
const src = fs.readFileSync(path.join(root, "static/src/kit/rendiciones_v2.js"), "utf8");
const start = src.indexOf("// confirm:start");
const end = src.indexOf("// confirm:end");
if (start < 0 || end < 0) throw new Error("confirm block not found");
const askFirst = new Function(src.slice(start, end) + "\nreturn askFirst;")();

function fakeForm(attrs, withYes) {
  const yes = withYes ? { value: "" } : null;
  const buttons = [{ disabled: true }];
  const form = {
    sent: 0,
    getAttribute: (k) => (k in attrs ? attrs[k] : null),
    querySelector: (sel) => (sel === 'input[name="confirmar"]' ? yes : null),
    querySelectorAll: () => buttons,
    submit() { this.sent += 1; },
  };
  return { form, yes, buttons };
}

function fakeEvent() {
  return { prevented: false, preventDefault() { this.prevented = true; } };
}

async function main() {
  const out = {};
  const asked = [];
  const answer = (value) => (text, ok) => { asked.push([text, ok]); return Promise.resolve(value); };

  // «Cancelar»
  let f = fakeForm({ "data-rd-confirm": "¿Reabrir?", "data-rd-ok": "Sí, reabrir" }, true);
  let ev = fakeEvent();
  const cancelled = await askFirst(f.form, answer(false))(ev);
  out.stops_the_post = ev.prevented;
  out.asks_its_text = asked.length === 1 && asked[0][0] === "¿Reabrir?" && asked[0][1] === "Sí, reabrir";
  out.cancel_sends_nothing = cancelled === false && f.form.sent === 0 && f.yes.value === "";
  out.cancel_gives_buttons_back = f.buttons.every((b) => !b.disabled);

  // «Sí»
  f = fakeForm({ "data-rd-confirm": "¿Anular?" }, true);
  ev = fakeEvent();
  const yes = await askFirst(f.form, answer(true))(ev);
  out.yes_confirms_and_sends_once = yes === true && f.form.sent === 1 && f.yes.value === "1";
  out.default_ok_label = asked[1][1] === "Sí, seguir";

  // already confirmed: the browser sends it, nothing asked
  ev = fakeEvent();
  const again = askFirst(f.form, answer(true))(ev);
  out.confirmed_form_not_stopped = again === undefined && !ev.prevented && asked.length === 2;

  // a form without confirmar is never stopped
  f = fakeForm({ "data-rd-confirm": "x" }, false);
  ev = fakeEvent();
  out.form_without_field_not_stopped = askFirst(f.form, answer(true))(ev) === undefined && !ev.prevented;

  process.stdout.write(JSON.stringify(out));
}

main().catch((e) => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
