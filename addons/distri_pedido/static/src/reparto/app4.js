/* Delivery driver app, 19.0.1.129.16: «Pagado / comprobante cargado» in the
 * visit (models/reparto_pagos.py).
 *
 * Loaded after app3.js, which works alone as before (a cached older page
 * never loads this file). app3.js exposes window.RepartoApp and calls the
 * hooks of window.RepartoExt:
 *   · noticeHtml(stop, extra): the green or amber notice of the visit, from
 *     stop.payments (downloaded with the route);
 *   · onLoadPoll(route): the payments that came with /reparto/api/load while
 *     loading the truck.
 * After the departure this file polls /reparto/api/load itself while a
 * visit or the visits list is on screen, so the notice shows without
 * «Actualizar la ruta».
 * «Ver comprobante» opens the receipt fullscreen (/reparto/api/receipt,
 * online only). Only informs: it changes no amount of the visit.
 */
(function () {
  "use strict";

  var APP = window.RepartoApp;
  if (!APP) return;

  var API_RECEIPT = "/reparto/api/receipt";
  var POLL_MS = 30000;
  var MIN_GAP_MS = 10000;
  var lastPoll = 0;
  var polling = false;

  function h(v) { return APP.h(v); }
  function money(n) { return APP.money(n); }

  function receiptLinks(stop, items) {
    var withFile = items.filter(function (p) { return p.has_file; });
    if (!withFile.length) return "";
    return " · " + withFile.map(function (p, i) {
      return '<button type="button" class="pay-link" data-a4="receipt" data-stop="' + stop.id +
        '" data-receipt="' + p.receipt_id + '">Ver comprobante' +
        (withFile.length > 1 ? " " + (i + 1) : "") + "</button>";
    }).join(" · ");
  }

  function sum(items) {
    return items.reduce(function (total, p) { return total + (Number(p.amount) || 0); }, 0);
  }

  function noticeHtml(stop, extra) {
    var payments = stop && stop.payments;
    if (!payments || !payments.length) return "";
    var base = "notice" + (extra ? " " + extra : "");
    var credited = payments.filter(function (p) { return p.state === "acreditado"; });
    var pending = payments.filter(function (p) { return p.state === "sin_acreditar"; });
    var out = "";
    if (credited.length) {
      var last = credited[0];
      var verified = credited.every(function (p) { return p.verified; });
      var text;
      if (stop.payments_paid) {
        text = "<b>Pagado por transferencia: " + h(money(sum(credited))) + "</b>" +
          (last.by ? " · acreditó " + h(last.by) : "") +
          (last.when_label ? " " + h(last.when_label) : "");
      } else {
        text = "<b>Pagó " + h(money(sum(credited))) + " por transferencia.</b> Saldo a cobrar: " +
          h(money(stop.payments_due || 0));
      }
      out += '<div class="' + base + ' pay-ok">' + text + receiptLinks(stop, credited) +
        (verified ? '<span class="pay-check">✓ Verificado con Mercado Pago</span>' : "") + "</div>";
    }
    if (pending.length) {
      out += '<div class="' + base + ' pay-wait"><b>Comprobante cargado, sin acreditar: ' +
        h(money(sum(pending))) + "</b>" + receiptLinks(stop, pending) +
        ". Todavía no bajó el saldo.</div>";
    }
    return out;
  }

  // ------------------------------------------------------------ polling

  function onScreen() {
    var part = location.hash.replace(/^#/, "").split("/")[0];
    return part === "visita" || part === "visitas" || part === "";
  }

  function typing() {
    var el = document.activeElement;
    return !!el && /^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName || "");
  }

  function applyPayments(map) {
    var route = APP.route();
    if (!map || !route) return;
    var changed = false;
    Object.keys(map).forEach(function (id) {
      var stop = APP.stopById(id), info = map[id];
      if (!stop || !info) return;
      var before = JSON.stringify([stop.payments || [], stop.payments_due || 0, !!stop.payments_paid]);
      var after = JSON.stringify([info.payments || [], info.due || 0, !!info.paid]);
      if (before === after) return;
      stop.payments = info.payments || [];
      stop.payments_due = info.due || 0;
      stop.payments_paid = !!info.paid;
      changed = true;
    });
    if (!changed) return;
    APP.saveRoute();
    if (onScreen() && !typing()) APP.rerender();
  }

  function onLoadPoll(route) {
    if (route && route.payments) applyPayments(route.payments);
  }

  function poll(force) {
    var route = APP.route();
    if (!route || polling || document.visibilityState !== "visible") return;
    // before the departure app3.js polls the same endpoint (onLoadPoll)
    if (!APP.departed() || !onScreen()) return;
    var now = Date.now();
    if (!force && now - lastPoll < POLL_MS - 500) return;
    if (now - lastPoll < MIN_GAP_MS) return;
    lastPoll = now;
    polling = true;
    var routeId = route.id;
    APP.fetchWithTimeout(APP.api.load + "?id=" + routeId, { credentials: "same-origin", cache: "no-store" }, 10000)
      .then(function (res) {
        if (APP.isLoginPage(res)) throw new Error("auth");
        return res.json();
      })
      .then(function (data) {
        var current = APP.route();
        if (data && data.ok && data.route && current && current.id === routeId) {
          applyPayments(data.route.payments);
        }
      })
      .catch(function () {})
      .then(function () { polling = false; });
  }

  setInterval(function () { poll(false); }, POLL_MS);
  window.addEventListener("hashchange", function () { setTimeout(function () { poll(true); }, 300); });
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") poll(true);
  });

  // ------------------------------------------------------------ receipt

  function toast(text) {
    var el = document.getElementById("toast");
    if (!el) return;
    el.textContent = text;
    el.className = "err";
    el.hidden = false;
    clearTimeout(toast.t);
    toast.t = setTimeout(function () { el.hidden = true; }, 3500);
  }

  function closeViewer() {
    var el = document.getElementById("rviewer");
    if (!el) return;
    var img = el.querySelector("img");
    if (img && img.src.indexOf("blob:") === 0) URL.revokeObjectURL(img.src);
    el.parentNode.removeChild(el);
  }

  function openViewer(stopId, receiptId) {
    var url = API_RECEIPT + "?stop=" + encodeURIComponent(stopId) + "&receipt=" + encodeURIComponent(receiptId);
    if (navigator.onLine === false) {
      toast("Sin señal: el comprobante se ve en línea. Probá de nuevo cuando tengas señal.");
      return;
    }
    closeViewer();
    var el = document.createElement("div");
    el.id = "rviewer";
    el.className = "rviewer";
    el.innerHTML = '<div class="rbar"><span>Comprobante</span>' +
      '<button type="button" data-a4="receipt-close">Cerrar</button></div>' +
      '<div class="rbody"><div class="rmsg">Cargando el comprobante…</div></div>';
    document.body.appendChild(el);
    APP.fetchWithTimeout(url, { credentials: "same-origin", cache: "no-store" }, 30000)
      .then(function (res) {
        if (!res.ok) throw new Error(res.status === 404 ? "Ese comprobante ya no está en esta visita." : "No se pudo abrir el comprobante.");
        var type = res.headers.get("Content-Type") || "";
        if (type.indexOf("text/html") === 0) throw new Error("Tu sesión venció: volvé a entrar con tu usuario.");
        return res.blob().then(function (blob) { return { blob: blob, type: type }; });
      })
      .then(function (file) {
        var body = el.querySelector(".rbody");
        if (!body || !document.getElementById("rviewer")) return;
        if (file.type.indexOf("image/") === 0) {
          var img = document.createElement("img");
          img.alt = "Comprobante";
          img.src = URL.createObjectURL(file.blob);
          body.innerHTML = "";
          body.appendChild(img);
        } else {
          // a PDF opens in the phone's viewer
          body.innerHTML = '<div class="rmsg">Es un PDF: se abre aparte.</div>';
          window.open(url, "_blank", "noopener");
        }
      })
      .catch(function (err) {
        var body = el.querySelector(".rbody");
        if (!body) return;
        var msg = err && err.message && err.name !== "AbortError" && err.name !== "TypeError" ? err.message :
          "Sin señal: el comprobante se ve en línea. Probá de nuevo cuando tengas señal.";
        body.innerHTML = '<div class="rmsg">' + h(msg) + "</div>";
      });
  }

  document.addEventListener("click", function (ev) {
    var el = ev.target.closest && ev.target.closest("[data-a4]");
    if (!el) return;
    ev.preventDefault();
    var a = el.getAttribute("data-a4");
    if (a === "receipt") openViewer(el.getAttribute("data-stop"), el.getAttribute("data-receipt"));
    else if (a === "receipt-close") closeViewer();
  });
  window.addEventListener("hashchange", closeViewer);

  window.RepartoExt = { noticeHtml: noticeHtml, onLoadPoll: onLoadPoll };
  // the screen was drawn before this file ran: draw it again with the notices
  if (APP.route()) APP.rerender();
})();
