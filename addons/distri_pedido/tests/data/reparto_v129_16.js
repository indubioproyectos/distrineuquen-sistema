// Runs the driver app's extension (static/src/reparto/app4.js) in a Node vm
// with a fake app3.js (window.RepartoApp) and prints the notices of the
// visit as JSON. Used by tests/test_v129_16_reparto_pagos.py.
//   node reparto_v129_16.js <module root>
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = process.argv[2];
const src = fs.readFileSync(path.join(root, "static/src/reparto/app4.js"), "utf8");

function money(n) {
  const v = Math.round((Number(n) || 0) * 100) / 100;
  const parts = Math.abs(v).toFixed(2).split(".");
  const int = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ".");
  return "$ " + (v < 0 ? "-" : "") + int + (parts[1] === "00" ? "" : "," + parts[1]);
}
function h(v) {
  return String(v == null ? "" : v).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
const stops = {};
let rerenders = 0;
const noop = () => {};
const window = {
  addEventListener: noop, open: noop,
  RepartoApp: {
    route: () => ({ id: 1, stops: Object.values(stops) }),
    stopById: (id) => stops[+id],
    online: () => true,
    departed: () => true,
    saveRoute: () => Promise.resolve(),
    rerender: () => { rerenders += 1; },
    money: money, h: h,
    fetchWithTimeout: () => Promise.reject(new TypeError("offline")),
    isLoginPage: () => false,
    api: { load: "/reparto/api/load" },
  },
};
const ctx = {
  console, Promise, Math, JSON, Date, Number, String, Object, Array, encodeURIComponent,
  window: window,
  setInterval: noop, setTimeout: noop, clearTimeout: noop,
  document: { visibilityState: "hidden", addEventListener: noop, activeElement: null,
              getElementById: () => null },
  location: { hash: "#visita/1" },
  navigator: { onLine: true },
  URL: URL,
};
vm.createContext(ctx);
vm.runInContext(src, ctx);
const ext = window.RepartoExt;

function payment(state, amount, extra) {
  return Object.assign({ receipt_id: 7, state: state, amount: amount, when_label: "14:05", by: "Gonzalo",
                         verified: false, has_file: true }, extra || {});
}
stops[1] = { id: 1, payments: [payment("acreditado", 2000, { verified: true })], payments_due: 0,
             payments_paid: true };
stops[2] = { id: 2, payments: [payment("acreditado", 1200)], payments_due: 800, payments_paid: false };
stops[3] = { id: 3, payments: [payment("sin_acreditar", 1500)], payments_due: 2000, payments_paid: false };
stops[4] = { id: 4, payments: [], payments_due: 0, payments_paid: false };
stops[5] = { id: 5 };  // a route downloaded by the older server: no payments at all

const out = {
  paid: ext.noticeHtml(stops[1], "place-notice"),
  partial: ext.noticeHtml(stops[2]),
  pending: ext.noticeHtml(stops[3]),
  none: ext.noticeHtml(stops[4]),
  old: ext.noticeHtml(stops[5]),
};
// the poll brings a new receipt for stop 4: the screen is drawn again once
const drawn = rerenders;
ext.onLoadPoll({ payments: { "4": { payments: [payment("sin_acreditar", 300)], due: 300, paid: false } } });
out.polled = ext.noticeHtml(stops[4]);
out.rerenders = rerenders - drawn;
process.stdout.write(JSON.stringify(out));
