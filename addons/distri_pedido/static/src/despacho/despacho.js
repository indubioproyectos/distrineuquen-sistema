/* «Despacho»: the dispatcher's screen to check the truck's load.
 *
 * Online only (there is a connection at the depot): every tick goes straight to the server
 * and the open route is polled every few seconds, so the driver's ticks from
 * the phone show up here. Rules and texts come from the server
 * (models/despacho.py, controllers/despacho.py).
 *
 * 19.0.1.119.0: when the route's customers have other open orders that are
 * not on it, «Controlado» comes back with `confirm` (the list) instead of
 * closing the dispatch; a sheet shows them and «Controlar igual» sends it
 * again with `confirmed: true`.
 */
(function () {
  "use strict";

  var API = "/despacho/api/";
  var POLL_MS = 4000;

  var S = {
    routes: null,     // the list of routes to dispatch
    route: null,      // the open route
    online: true,
    auth: true,
    busy: false,      // an action is on its way
    dialog: null,     // «Bajar al depósito»: { stop }; or «Controlado» to confirm: { confirm: [notes] }
    error: null,      // the last refusal of «Controlado»
    user: "",
  };

  function h(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function plural(n, one, many) { return n + " " + (n === 1 ? one : many); }

  var ICON_CHECK = '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5L20 7"></path></svg>';
  var ICON_NOSIGNAL = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 8.8a15 15 0 0 1 20 0"></path><path d="M5 12.5a10 10 0 0 1 14 0"></path><path d="M8.5 16.4a5 5 0 0 1 7 0"></path><path d="M3 3l18 18"></path></svg>';

  // ------------------------------------------------------------- server

  function request(url, options) {
    options = options || {};
    options.credentials = "same-origin";
    options.cache = "no-store";
    var ctrl = window.AbortController ? new AbortController() : null;
    var timer = ctrl ? setTimeout(function () { ctrl.abort(); }, 15000) : null;
    if (ctrl) options.signal = ctrl.signal;
    return fetch(url, options).then(function (res) {
      clearTimeout(timer);
      var type = res.headers.get("Content-Type") || "";
      if (res.redirected || type.indexOf("application/json") < 0) {
        S.auth = false;
        throw new Error("auth");
      }
      S.online = true; S.auth = true;
      return res.json();
    }, function (err) {
      clearTimeout(timer);
      S.online = false;
      throw err;
    });
  }

  function currentId() {
    var m = location.hash.match(/^#ruta\/(\d+)/);
    return m ? +m[1] : null;
  }

  function refresh() {
    var id = currentId();
    var url = id ? API + "route?id=" + id : API + "routes";
    return request(url).then(function (data) {
      if (!data.ok) throw new Error(data.error || "Error");
      if (id) {
        S.route = data.route;
      } else {
        S.routes = data.routes; S.user = data.user || "";
      }
    }).catch(function (err) {
      if (err && err.message !== "auth" && S.online) toast(err.message, true);
    }).then(function () { if (!S.dialog) render(); else paintStatus(); });
  }

  function act(body) {
    if (S.busy) return Promise.resolve();
    if (!S.online) { toast("Sin conexión. Conectate a internet y probá de nuevo.", true); return Promise.resolve(); }
    S.busy = true;
    body.route_id = S.route.id;
    return request(API + "action", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    }).then(function (data) {
      if (data.route) S.route = data.route;
      if (!data.ok && data.confirm) {
        // other open orders of the route's customers: the dispatcher decides
        S.error = null;
        S.dialog = { confirm: data.confirm };
      } else if (!data.ok) {
        if (body.action === "controlled") S.error = data.error;
        else toast(data.error || "No se pudo guardar.", true);
      } else {
        // what «Controlado» said is old news after any change
        S.error = null;
        if (body.action === "controlled") toast("Salida controlada.");
      }
      return data;
    }).catch(function () {
      toast("No se pudo guardar: sin conexión con el servidor.", true);
    }).then(function (data) {
      S.busy = false;
      render();
      return data;
    });
  }

  // ------------------------------------------------------------- render

  var app = document.getElementById("app");

  function toast(text, isError) {
    var el = document.getElementById("toast");
    el.textContent = text;
    el.className = isError ? "err" : "";
    el.hidden = false;
    clearTimeout(toast.t);
    toast.t = setTimeout(function () { el.hidden = true; }, 4000);
  }

  function statusBanner() {
    if (!S.auth) {
      return '<div class="banner err">Tu sesión venció. <a href="/web/login?redirect=/despacho">Entrar de nuevo</a></div>';
    }
    if (!S.online) {
      return '<div class="banner err">' + ICON_NOSIGNAL + "Sin conexión. Conectate a internet y probá de nuevo: " +
        "los tildes no se guardan sin conexión.</div>";
    }
    return "";
  }
  function paintStatus() {
    var el = document.getElementById("status");
    if (el) el.innerHTML = statusBanner();
  }

  function render() {
    var y = window.scrollY;
    app.innerHTML = currentId() ? viewRoute() : viewRoutes();
    if (S.dialog) app.insertAdjacentHTML("beforeend", viewDialog());
    window.scrollTo(0, y);
  }

  function viewRoutes() {
    var head = '<div class="head"><div class="kicker">Despacho</div><h1>Control de carga</h1>' +
      '<div class="sub">' + h(S.user) + " · cada pedido lo tildan el repartidor y vos</div></div>" +
      '<div id="status">' + statusBanner() + "</div>";
    if (!S.routes) return head + '<div class="body"><div class="cargando">Cargando…</div></div>';
    var cards = S.routes.map(function (r) {
      var done = r.orders_total && r.orders_checked === r.orders_total;
      return '<a class="route-card' + (r.is_driver ? " mine" : "") + '" href="#ruta/' + r.id + '">' +
        '<div class="split"><span class="t">' + h(r.driver) + "</span>" +
        '<span class="pill ' + (r.controlled ? "ok" : done ? "ok" : "warn") + '">' +
        (r.controlled ? "CONTROLADO" : r.orders_checked + " DE " + r.orders_total) + "</span></div>" +
        '<div class="d">Viaje ' + h(r.trip) + " · " + h(r.day) + (r.zones ? " · " + h(r.zones) : "") + "</div>" +
        '<div class="d">' + plural(r.orders_total, "pedido", "pedidos") + " · " + r.orders_checked +
        " con doble control" + (r.is_driver ? " · sos el repartidor" : "") + "</div></a>";
    }).join("");
    return head + '<div class="body">' +
      (cards || '<div class="card">No hay rutas asignadas esperando salir.</div>') + "</div>";
  }

  function stopRow(s, route) {
    var cls = "row" + (s.load_state === "both" ? " both" : "") + (s.mismatch ? " mismatch" : "");
    var can = route.editable && S.online && !S.busy;
    var stCls = s.load_state === "both" ? "ok" : s.mismatch ? "warn" : "none";
    var drv = s.loaded ? route.driver_first + " ✓" + (s.load_sequence ? " (" + s.load_sequence + ".º en subir)" : "")
                       : route.driver_first + " sin tildar";
    return '<div class="' + cls + '">' +
      '<button class="tick' + (s.dispatch_checked ? " on" : "") + '" data-a="tick" data-id="' + s.id + '"' +
      (can ? "" : " disabled") + ' aria-label="Controlado por el depósito">' + (s.dispatch_checked ? ICON_CHECK : "") + "</button>" +
      '<div class="who"><b>' + h(s.name) + "</b>" +
      '<span class="d">' + h([s.address, s.order].filter(Boolean).join(" · ")) + "</span>" +
      '<div class="st ' + stCls + '"><span class="drv' + (s.loaded ? " on" : "") + '">' + h(drv) + "</span>" +
      "</div>" + '<div class="st ' + stCls + '">' + h(s.message) + "</div>" +
      (route.editable ? '<button class="leave" data-a="leave" data-id="' + s.id + '"' + (can ? "" : " disabled") +
        ">Bajar al depósito</button>" : "") + "</div>" +
      '<div class="side"><span class="n">' + plural(s.parcels || 0, "bulto", "bultos") + "</span>" +
      (s.packed ? '<a class="link" target="_blank" rel="noopener" href="/armado/pedido/' + s.order_id +
        '/etiquetas">Etiquetas</a>' : "") + "</div></div>";
  }

  function viewRoute() {
    var r = S.route;
    if (!r || r.id !== currentId()) {
      return '<div class="head"><button class="back" data-a="back">‹ Rutas</button><h1>Despacho</h1></div>' +
        '<div id="status">' + statusBanner() + '</div><div class="body"><div class="cargando">Cargando…</div></div>';
    }
    var ordersDone = r.orders_total > 0 && r.orders_checked === r.orders_total;
    var pct = r.orders_total ? Math.round(r.orders_checked * 100 / r.orders_total) : 0;
    var out = '<div class="head"><button class="back" data-a="back">‹ Rutas</button>' +
      '<div class="kicker">Despacho · Viaje ' + h(r.trip) + " · " + h(r.day) + "</div>" +
      "<h1>" + h(r.driver) + '</h1><div class="sub">' + h(r.zones_text || "Sin zonas") + "</div></div>" +
      '<div id="status">' + statusBanner() + "</div>";
    if (r.is_driver && r.self_dispatch) {
      // 19.0.1.127.0: administración/gerencia control their own load (noted on the route)
      out += '<div class="banner info">Sos el repartidor de esta ruta: podés controlar tu propia carga. ' +
        "Queda anotado como autodespacho.</div>";
    } else if (r.is_driver) {
      out += '<div class="banner warn">Sos el repartidor de esta ruta: la carga la controla otra persona.</div>';
    }
    if (r.controlled) {
      out += '<div class="banner ok">' + ICON_CHECK + "Controlado por " + h(r.controlled.by) + " a las " +
        h(r.controlled.at) + "</div>";
    } else if (r.state !== "draft") {
      out += '<div class="banner info">La ruta ya salió.</div>';
    }
    out += '<div class="counters">' +
      '<div class="counter' + (ordersDone ? " done" : "") + '"><span>Pedidos con doble control</span><b>' +
      r.orders_checked + " <small>de " + r.orders_total + "</small></b></div>" +
      '<div class="counter' + (ordersDone ? " done" : "") + '"><span>Bultos con doble control</span><b>' +
      r.parcels_checked + " <small>de " + r.parcels_total + "</small></b></div></div>" +
      '<div class="bar"><span style="width:' + pct + '%"></span></div>';
    out += '<div class="body">';
    if (!r.zones.length) out += '<div class="card">La ruta no tiene pedidos.</div>';
    r.zones.forEach(function (z) {
      var both = z.stops.filter(function (s) { return s.load_state === "both"; }).length;
      out += '<div class="zone"><span>' + h(z.name) + "</span><span>" + both + " de " + z.stops.length + "</span></div>";
      out += z.stops.map(function (s) { return stopRow(s, r); }).join("");
    });
    if (r.left.length) {
      out += '<div class="zone"><span>Quedan en el depósito</span><span>' + r.left.length + "</span></div>";
      out += r.left.map(function (l) {
        return '<div class="left"><div class="who"><b>' + h(l.name) + "</b><span>" + h(l.order) + " · " +
          plural(l.parcels || 0, "bulto", "bultos") + "</span><span>Motivo: " + h(l.reason) +
          (l.by ? " · lo marcó " + h(l.by) : "") + "</span></div>" +
          (r.editable ? '<button class="link" data-a="undo" data-id="' + l.id + '"' + (S.online && !S.busy ? "" : " disabled") +
            ">Deshacer</button>" : "") + "</div>";
      }).join("");
    }
    out += "</div>";
    if (!r.controlled && r.state === "draft") {
      var missing = r.orders_total - r.orders_checked;
      out += '<div class="foot">' +
        (S.error ? '<div class="banner err" style="border-radius:10px;padding:10px 12px;white-space:pre-line;display:block">' +
          h(S.error) + "</div>" : "") +
        '<button class="btn' + (ordersDone ? " teal" : "") + '" data-a="controlled"' +
        (r.editable && S.online && !S.busy ? "" : " disabled") + ">Controlado</button>" +
        '<div class="hint">' + (ordersDone ? "Todo controlado por los dos. Tocá «Controlado» para cerrar el despacho." :
          "Faltan " + plural(missing, "pedido", "pedidos") + " con doble control.") + "</div></div>";
    }
    return out;
  }

  function viewConfirm() {
    var notes = S.dialog.confirm || [];
    return '<div class="sheet-bg" data-a="dlg-bg"><div class="sheet" role="dialog" aria-modal="true">' +
      "<h2>¿Controlar igual?</h2>" +
      '<div class="sub">' + (notes.length === 1 ? "Un cliente de esta ruta tiene otro pedido que no va cargado:" :
        "Clientes de esta ruta tienen otros pedidos que no van cargados:") + "</div>" +
      '<ul class="confirm-list">' + notes.map(function (n) { return "<li>" + h(n) + "</li>"; }).join("") + "</ul>" +
      '<div class="grid2"><button class="btn outline" data-a="dlg-cancel">Volver</button>' +
      '<button class="btn teal" data-a="dlg-force"' + (S.online && !S.busy ? "" : " disabled") +
      ">Controlar igual</button></div>" +
      "</div></div>";
  }

  // 19.0.1.119.0: one tap and one confirmation, no reason to type (the
  // server notes «Bajado en el despacho»)
  function viewDialog() {
    if (S.dialog.confirm) return viewConfirm();
    var s = S.dialog.stop;
    return '<div class="sheet-bg" data-a="dlg-bg"><div class="sheet" role="dialog" aria-modal="true">' +
      "<h2>¿Bajar " + h(s.order) + " (" + h(s.name) + ") al depósito?</h2>" +
      '<div class="sub">Vuelve a la cola de Sin ruta.</div>' +
      '<div class="grid2"><button class="btn outline" data-a="dlg-cancel">Cancelar</button>' +
      '<button class="btn red" data-a="dlg-ok"' + (S.online && !S.busy ? "" : " disabled") +
      ">Bajar al depósito</button></div>" +
      "</div></div>";
  }

  function findStop(id) {
    var found = null;
    (S.route ? S.route.zones : []).forEach(function (z) {
      z.stops.forEach(function (s) { if (s.id === id) found = s; });
    });
    return found;
  }

  // ------------------------------------------------------------- events

  var ACTIONS = {
    back: function () { S.error = null; location.hash = ""; },
    tick: function (el) {
      var s = findStop(+el.getAttribute("data-id"));
      if (s) act({ action: s.dispatch_checked ? "uncheck" : "check", stop_id: s.id });
    },
    leave: function (el) {
      var s = findStop(+el.getAttribute("data-id"));
      if (!s) return;
      S.dialog = { stop: s };
      render();
    },
    "dlg-bg": function (el, ev) { if (ev.target === el) ACTIONS["dlg-cancel"](); },
    "dlg-cancel": function () { S.dialog = null; render(); },
    "dlg-ok": function () {
      var d = S.dialog;
      if (!d || !d.stop) return;
      act({ action: "leave", stop_id: d.stop.id }).then(function (data) {
        if (data && data.ok) { S.dialog = null; toast(d.stop.order + " volvió al depósito, sin ruta."); render(); }
      });
    },
    undo: function (el) {
      act({ action: "undo", left_id: +el.getAttribute("data-id") }).then(function (data) {
        if (data && data.ok) toast("Volvió a la ruta: hay que tildarlo de nuevo.");
      });
    },
    controlled: function () { act({ action: "controlled" }); },
    "dlg-force": function () {
      act({ action: "controlled", confirmed: true }).then(function (data) {
        if (S.dialog && S.dialog.confirm && !(data && data.confirm)) { S.dialog = null; render(); }
      });
    },
  };

  document.addEventListener("click", function (ev) {
    var el = ev.target.closest("[data-a]");
    if (!el || el.disabled) return;
    var handler = ACTIONS[el.getAttribute("data-a")];
    if (!handler) return;
    ev.preventDefault();
    handler(el, ev);
  });
  window.addEventListener("hashchange", function () {
    S.error = null; S.dialog = null;
    if (!currentId()) S.route = null;
    render(); window.scrollTo(0, 0); refresh();
  });
  window.addEventListener("online", function () { refresh(); });
  window.addEventListener("offline", function () { S.online = false; paintStatus(); render(); });
  setInterval(function () {
    if (document.visibilityState === "visible" && !S.busy && S.auth) refresh();
  }, POLL_MS);
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") refresh();
  });

  render();
  refresh();
})();
