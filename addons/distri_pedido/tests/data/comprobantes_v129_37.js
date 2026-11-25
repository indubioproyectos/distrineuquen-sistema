// 19.0.1.129.37: the browser side of the multi-file receipt upload
// (static/src/comprobantes/comprobantes.js: fileProblem, uploadEach) and of
// «Subir comprobante» at the depot (static/src/deposito/deposito.js:
// fileProblem). Runs the functions on their own and prints the checks as
// JSON. Used by tests/test_v129_37_comprobantes.py.
//   node comprobantes_v129_37.js <module root>
"use strict";
const fs = require("fs");
const path = require("path");

const root = process.argv[2];
const read = (p) => fs.readFileSync(path.join(root, p), "utf8");

function block(src, start, end) {
  const i = src.indexOf(start);
  const j = src.indexOf(end, i);
  if (i < 0 || j < 0) throw new Error("block not found: " + start);
  return src.slice(i, j + end.length);
}

function load(file, names) {
  const src = read(file);
  const consts = block(src, "  var RECEIPT_MAX_BYTES", "var RECEIPT_NAMES = /\\.(jpe?g|png|webp|pdf)$/i;");
  const parts = names.map((n) => block(src, "  function " + n + "(", "\n  }\n"));
  return new Function(consts + "\n" + parts.join("\n") +
                      "\nreturn {" + names.join(", ") + "};")();
}

const MB = 1024 * 1024;
const files = {
  jpg: { name: "IMG-20261125-WA0001.jpg", type: "image/jpeg", size: 200 * 1024 },
  pdf: { name: "Comprobante de transferencia.pdf", type: "application/pdf", size: 90 * 1024 },
  webpNoType: { name: "captura.webp", type: "", size: 50 * 1024 },
  heic: { name: "IMG_0001.HEIC", type: "image/heic", size: 300 * 1024 },
  txt: { name: "nota.txt", type: "text/plain", size: 10 },
  big: { name: "escaneo.pdf", type: "application/pdf", size: 11 * MB },
};

function problems(fileProblem) {
  const out = {};
  for (const [key, f] of Object.entries(files)) out[key] = fileProblem(f);
  return out;
}

async function each() {
  const { uploadEach, fileProblem } = load("static/src/comprobantes/comprobantes.js",
                                          ["fileProblem", "uploadEach"]);
  const items = [files.jpg, files.txt, files.pdf, files.webpNoType].map((f) => (
    { file: f, problem: fileProblem(f) }));
  const posted = [];
  const steps = [];
  let running = 0, overlap = false;
  const results = await uploadEach(items, (item) => {
    running += 1;
    if (running > 1) overlap = true;
    posted.push(item.file.name);
    return new Promise((resolve, reject) => setTimeout(() => {
      running -= 1;
      if (item.file === files.pdf) { reject(new Error("timeout")); return; }
      resolve({ name: item.file.name, status: "nuevo", message: "Subido." });
    }, 5));
  }, (item, n, result) => steps.push([n, result.status]));
  return {
    problems: problems(fileProblem),
    posted,
    overlap,
    statuses: results.map((r) => r.status),
    steps,
    failedMessage: results[2].message,
    empty: (await uploadEach([], () => Promise.resolve({}), () => {})).length,
  };
}

function depot() {
  const { fileProblem } = load("static/src/deposito/deposito.js", ["fileProblem"]);
  return { problems: problems(fileProblem) };
}

(async () => {
  const out = { each: await each(), depot: depot() };
  process.stdout.write(JSON.stringify(out));
})().catch((e) => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
