// 19.0.1.129.42 (item 2): the pure helpers of static/src/armado/armado.js.
// Prints the checks as JSON. Used by tests/test_v129_42_armado.py.
//   node armado_v129_42.js <module root>
// - destino: a finished order's own page replaces the current one, anything
//   else goes up to the list (DN.up) as before;
// - mostrar: a card shows when it matches every word and the state chip.
"use strict";
const fs = require("fs");
const path = require("path");

const root = process.argv[2];
const window = {};
const document = { body: { getAttribute: () => "" }, addEventListener() {} };
new Function("window", "document", fs.readFileSync(path.join(root, "static/src/armado/armado.js"), "utf8"))(
  window, document);
const A = window.DNArmado;
const out = {
  destinoRecien: A.destino("/armado/pedido/12?recien=1"),
  destinoLista: A.destino("/armado?terminado=12"),
  destinoDejado: A.destino("/armado?dejado=12"),
  destinoVacio: A.destino(""),
  todos: A.mostrar("almacen ana s00012", [], "", "armado"),
  palabra: A.mostrar("almacen ana s00012", ["ana"], "", "pendiente"),
  palabraNo: A.mostrar("almacen ana s00012", ["luis"], "", "pendiente"),
  chip: A.mostrar("almacen ana s00012", [], "armado", "armado"),
  chipNo: A.mostrar("almacen ana s00012", [], "armado", "pendiente"),
  ambos: A.mostrar("almacen ana s00012", ["s00012"], "en_armado", "en_armado"),
};
process.stdout.write(JSON.stringify(out));
