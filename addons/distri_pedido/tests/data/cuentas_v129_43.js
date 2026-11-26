// 19.0.1.129.43: the «list-v2» block of static/src/cuentas/cuentas_app.js
// (the one list of «Cuentas de clientes») against fake pages. Prints the
// checks as JSON. Used by tests/test_v129_43_cuentas.py.
//   node cuentas_v129_43.js <module root>
// - the query: no filter on by default, offset/limit, the zones only once;
// - the sheet's filter count, the 2-letter search, the status pills;
// - the ⋯ menu: secondary actions only, PDF/Excel only when allowed;
// - paging: next() asks the following offset once; a newer reload drops
//   the late answers of an older one; nothing after the last page.
"use strict";
const fs = require("fs");
const path = require("path");

const root = process.argv[2];
const src = fs.readFileSync(path.join(root, "static/src/cuentas/cuentas_app.js"), "utf8");
const start = src.indexOf("// list-v2:start");
const end = src.indexOf("// list-v2:end");
if (start < 0 || end < 0) throw new Error("list-v2 block not found");
const block = src.slice(start, end);
const api = new Function(block + "\nreturn { LIST_LIMIT, LIST_DEFAULTS, listFilters, listQuery, " +
  "sheetFilterCount, searchTerm, statePill, menuItems, makeList };")();
const tick = () => new Promise((r) => setImmediate(r));

function deferred() {
  let resolve, reject;
  const p = new Promise((a, b) => { resolve = a; reject = b; });
  return { p, resolve, reject };
}

async function main() {
  const out = {};
  const f = api.listFilters();
  const q = api.listQuery(f);
  out.default_query = q.indexOf("state=all") >= 0 && q.indexOf("sort=debt") >= 0 && q.indexOf("q=&") === 0 &&
    q.indexOf("seller=all") >= 0 && q.indexOf("min_balance=0") >= 0 && q.indexOf("min_age=0") >= 0 &&
    /offset=0&limit=50$/.test(q);
  out.zones_only_when_asked = api.listQuery(f, 0, 50, true).endsWith("&zones=1") && q.indexOf("zones") < 0;
  out.count_query = /offset=0&limit=0$/.test(api.listQuery(f, 0, 0));
  out.defaults_have_no_filter = api.sheetFilterCount(f) === 0 && f.state === "all" && f.q === "";
  out.filter_count = api.sheetFilterCount(api.listFilters({ sort: "oldest", zone: "Centenario", minAge: "30" })) === 3;
  out.search_from_two_letters = api.searchTerm("a") === "" && api.searchTerm("  ki  ") === "ki" &&
    api.searchTerm("la   esquina") === "la esquina";
  const pill = (d) => api.statePill(d).join("|");
  out.pills = pill({ state: "debt", oldest_owed_days: 38 }) === "err|Vencido 38 días" &&
    pill({ state: "debt", oldest_owed_days: 1 }) === "err|Vencido 1 día" &&
    pill({ state: "debt", oldest_owed_days: 0 }) === "|Con deuda" &&
    pill({ state: "zero" }) === "ok|Al día" && pill({ state: "credit" }) === "ok|A favor" &&
    pill({ state: "uncollectible" }) === "neutral|Incobrable";

  const seller = api.menuItems({ can_export: false, can_see_transfers: false, last_load: null }, "all");
  const labels = seller.filter((i) => !i.sep).map((i) => i.label);
  out.menu_seller = labels.join(",") === "Actualizar,Cobro en efectivo,Salir";
  const admin = api.menuItems({ can_export: true, can_see_transfers: true, last_load: { receipts_until: "x" } }, "italo");
  const pdf = admin.find((i) => i.label === "Descargar PDF");
  const xls = admin.find((i) => i.label === "Descargar Excel");
  out.menu_admin = !!pdf && pdf.href === "/cuentas/deudores.pdf?seller=italo" && pdf.blank === true &&
    !!xls && xls.href === "/cuentas/deudores.xlsx?seller=italo" &&
    admin.some((i) => i.href === "#transferencias") && admin.some((i) => i.id === "cu-lastload") &&
    admin[admin.length - 1].label === "Salir" && admin[0].label === "Actualizar";

  // paging and stale answers
  const asked = [];
  const pending = [];
  let changes = 0;
  const errors = [];
  const list = api.makeList((query) => {
    asked.push(query);
    const d = deferred();
    pending.push(d);
    return d.p;
  }, () => { changes += 1; }, (err) => errors.push(err.message));
  const rows = (from, n) => Array.from({ length: n }, (_, i) => ({ id: from + i }));

  list.reload(api.listFilters());
  out.first_page = asked.length === 1 && /offset=0&limit=50&zones=1$/.test(asked[0]) && list.loading === true;
  out.no_next_while_loading = list.next() === null && asked.length === 1;
  pending[0].resolve({ rows: rows(1, 50), more: true, total_rows: 120, zones: ["Plottier"] });
  await tick();
  out.first_taken = list.rows.length === 50 && list.more && !list.loading && list.zones[0] === "Plottier";
  list.next();
  out.next_offset = /offset=50&limit=50$/.test(asked[1]) && asked[1].indexOf("zones") < 0;
  out.next_once = list.next() === null && asked.length === 2;
  pending[1].resolve({ rows: rows(51, 50), more: true, total_rows: 120 });
  await tick();
  out.appended = list.rows.length === 100 && list.rows[99].id === 100 && list.data.total_rows === 120;

  // a new search while the 3rd page is on its way: the late page is dropped
  list.next();
  list.reload(api.listFilters({ q: "kiosco" }));
  out.reload_query = /^q=kiosco&/.test(asked[3]) && /offset=0/.test(asked[3]) && asked[3].indexOf("zones") < 0;
  pending[2].resolve({ rows: rows(101, 20), more: false, total_rows: 120 });
  await tick();
  out.stale_page_dropped = list.rows.length === 100 && list.loading === true;
  pending[3].resolve({ rows: rows(500, 2), more: false, total_rows: 2 });
  await tick();
  out.new_search_taken = list.rows.length === 2 && list.rows[0].id === 500 && !list.more && list.data.total_rows === 2;
  out.nothing_after_last = list.next() === null && asked.length === 4;

  // an error of an older reload is not shown; the current one is
  list.reload(api.listFilters({ state: "credit" }));
  list.reload(api.listFilters({ state: "zero" }));
  pending[4].reject(new Error("old"));
  pending[5].reject(new Error("new"));
  await tick();
  await tick();
  out.only_current_error = errors.length === 1 && errors[0] === "new" && list.loading === false;
  out.render_called = changes > 0;

  // 19.0.1.129.49: the PC account takes the whole page (desk-v2 block)
  const ds = src.indexOf("// desk-v2:start");
  const de = src.indexOf("// desk-v2:end");
  if (ds < 0 || de < 0) throw new Error("desk-v2 block not found");
  const desk = new Function(src.slice(ds, de) + "\nreturn { STMT_COLS, deskMode, deskScroll, stmtTable };")();
  out.desk_modes = desk.deskMode(true, true, { id: null }) === "list" &&
    desk.deskMode(true, true, { id: 7 }) === "detail" &&
    desk.deskMode(true, true, { id: null, transfers: true }) === "transfers" &&
    desk.deskMode(false, true, { id: 7 }) === "" && desk.deskMode(true, false, { id: 7 }) === "";
  // open an account from the list scrolled to 900: the account at the top,
  // the list's place kept; back to the list: 900 again
  let sc = desk.deskScroll(0, 0, 900, 0);
  out.desk_list_keeps_scroll = sc.y === 900;
  sc = desk.deskScroll(0, 7, 900, sc.saved);
  out.desk_detail_top = sc.y === 0 && sc.saved === 900;
  sc = desk.deskScroll(7, 7, 350, sc.saved);
  out.desk_detail_rerender_keeps = sc.y === 350 && sc.saved === 900;
  sc = desk.deskScroll(7, 0, 350, sc.saved);
  out.desk_back_restores = sc.y === 900;
  const fmt = { h: (t) => String(t == null ? "" : t).replace(/</g, "&lt;"), money: (n) => "$ " + n,
    day: (iso) => iso, opening: "Saldo anterior" };
  const html = desk.stmtTable([
    { line_key: "a1", date: "2026-11-20", doc_type: "Factura", number: "A 0001-00000123", remito: "R-9",
      concept: "Venta <x>", debit: 1500, credit: 0, balance: 1500, owed: 1500 },
    { line_key: "a2", date: "2026-11-22", doc_type: "Recibo", number: "", concept: "", source_label: "Transferencia",
      debit: 0, credit: 500, balance: 1000, confirmed: true },
  ], { more: false, opening: 0 }, fmt);
  const heads = (html.match(/<th[^>]*>([^<]*)<\/th>/g) || []).map((t) => t.replace(/<[^>]+>/g, ""));
  out.desk_stmt_columns = heads.join(",") === "Fecha,Documento,Descripción,Debe,Haber,Saldo";
  const firstRow = html.split("<tr")[2] || "";
  out.desk_stmt_row = (firstRow.match(/<td/g) || []).length === 6 && firstRow.indexOf("Factura A 0001-00000123") >= 0 &&
    firstRow.indexOf("Remito R-9 · Venta &lt;x>") >= 0 && firstRow.indexOf("debe $ 1500") >= 0;
  out.desk_stmt_credit = html.indexOf('<td class="r num cr">$ 500</td>') >= 0 && html.indexOf("Transferencia") >= 0;
  out.desk_stmt_opening = /<tr class="opening"><td colspan="5"[^>]*>Saldo anterior<\/td><td class="r num">\$ 0<\/td>/.test(html);
  out.desk_stmt_empty = desk.stmtTable([], { more: true }, fmt).indexOf('colspan="6"') >= 0;
  // the views: the PC account no longer renders beside the list
  const desk2 = src.slice(src.indexOf("function viewDesk2()"), src.indexOf("function viewSheet()"));
  out.desk_no_side_column = desk2.length > 100 && desk2.indexOf("deskAccount()") < 0 &&
    src.indexOf("r.id ? viewDeskAccount2() : viewDesk2()") >= 0 && src.indexOf("Volver a la lista") >= 0;
  process.stdout.write(JSON.stringify(out));
}

main().catch((err) => { process.stderr.write(String(err && err.stack || err)); process.exit(1); });
