/* The claim messages of the pending items (models/comprobante_pendiente.py
 * builds the same ones). Wording and rules come from claim_texts.json, sent
 * by the server with the route: this file only applies them, so the app
 * and Odoo cannot drift apart (tests/test_comprobantes.py runs this file
 * with Node and compares the messages).
 *
 * DistriClaim(texts) -> {relWhen, money, documentOf, itemLine, customerClaim}
 * An item: {kind: "done"|"unpaid", amount, document: "Factura 0001-…",
 *           delivered_at: "YYYY-MM-DD HH:MM:SS" (UTC) or ISO, state}.
 */
(function (root) {
  "use strict";
  var AR_OFFSET_MS = 3 * 3600000;  // Argentina, UTC-3 all year

  function make(T) {
    function fill(tpl, values) {
      return String(tpl).replace(/\{(\w+)\}/g, function (m, key) {
        return key in values ? String(values[key]) : m;
      });
    }
    function parseUtc(value) {
      if (!value) return null;
      var s = String(value);
      var ms = Date.parse(s.indexOf("T") >= 0 ? s : s.replace(" ", "T") + "Z");
      return isNaN(ms) ? null : ms;
    }
    function arDate(ms) { return new Date(ms - AR_OFFSET_MS); }  // read with getUTC*
    function dayNumber(d) { return Math.floor(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()) / 86400000); }
    function ddmm(d) { return ("0" + d.getUTCDate()).slice(-2) + "/" + ("0" + (d.getUTCMonth() + 1)).slice(-2); }

    // «$ 40.000» / «$ 40.000,50», like the server's _money
    function money(n) {
      var cents = Math.round((Number(n) || 0) * 100), neg = cents < 0;
      cents = Math.abs(cents);
      var whole = String(Math.floor(cents / 100)).replace(/\B(?=(\d{3})+(?!\d))/g, ".");
      var dec = ("0" + (cents % 100)).slice(-2);
      return "$ " + (neg ? "-" : "") + whole + (dec === "00" ? "" : "," + dec);
    }

    function relWhen(value, nowMs) {
      var ms = parseUtc(value);
      if (ms === null) return "";
      nowMs = nowMs || Date.now();
      var local = arDate(ms), now = arDate(nowMs), W = T.when;
      var days = dayNumber(now) - dayNumber(local);
      if (days <= 0) {
        if (nowMs - ms < T.recent_hours * 3600000) return W.recent;
        return local.getUTCHours() < T.afternoon_hour ? W.morning : W.afternoon;
      }
      if (days === 1) return W.yesterday;
      if (days <= 6) return fill(W.weekday, { weekday: T.weekdays[(local.getUTCDay() + 6) % 7], date: ddmm(local) });
      return fill(W.date, { date: ddmm(local) });
    }

    // «Factura 0001-…» -> its phrase «el pedido de la factura 0001-…»
    function phraseOf(document) {
      var D = T.documents, doc = String(document || "");
      var keys = Object.keys(D);
      for (var i = 0; i < keys.length; i++) {
        var label = D[keys[i]][0], prefix = label.split("{number}")[0];
        if (doc.indexOf(prefix) === 0) return fill(D[keys[i]][1], { number: doc.slice(prefix.length) });
      }
      return doc;
    }

    function itemLine(r, nowMs) {
      return fill(T.item_line, { amount: money(r.amount), document: r.document, when: relWhen(r.delivered_at, nowMs) });
    }

    function customerClaim(items, voice, nowMs, company) {
      var V = T.voices[voice || "driver"];
      items = (items || []).filter(function (r) { return r.state !== "fulfilled" && r.state !== "cancelled"; })
        .slice().sort(function (a, b) {
          return ((parseUtc(a.delivered_at) || 0) - (parseUtc(b.delivered_at) || 0)) || ((a.id || 0) - (b.id || 0));
        });
      if (!items.length) return "";
      var text;
      if (items.length === 1) {
        var r = items[0];
        text = fill(r.kind === "unpaid" ? V.single_unpaid : V.single_done, {
          phrase: phraseOf(r.document), when: relWhen(r.delivered_at, nowMs), amount: money(r.amount) });
      } else {
        var total = 0;
        var lines = items.map(function (x) {
          total += Math.round((Number(x.amount) || 0) * 100);
          return fill(x.kind === "unpaid" ? V.line_unpaid : V.line_done, {
            document: x.document, when: relWhen(x.delivered_at, nowMs), amount: money(x.amount) });
        });
        text = [V.multi_head].concat(lines).concat([fill(V.multi_tail, { total: money(total / 100) })]).join("\n");
      }
      return text + fill(V.signature || "", { company: company || "" });
    }

    return { relWhen: relWhen, money: money, phraseOf: phraseOf, itemLine: itemLine, customerClaim: customerClaim,
             kindLabel: function (kind) { return T.kind_labels[kind] || T.kind_labels.done; } };
  }

  root.DistriClaim = make;
  if (typeof module !== "undefined" && module.exports) module.exports = make;
})(typeof self !== "undefined" ? self : this);
