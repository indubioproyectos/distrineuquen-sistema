/* Delivery driver app, 19.0.1.129.28: «Reparto v2» (models/reparto_v2.py),
 * the owner-approved «cobro del repartidor» proposal, stages 1 and 2.
 *
 * Loaded after app3.js and app4.js, which work alone as before (a cached
 * older page never loads this file). Everything here runs only when the
 * downloaded route says `reparto_v2` (the server flag distri_pedido.reparto_v2)
 * or /reparto/api/home answers v2: with the flag off every hook returns
 * nothing and app3.js draws the screens of before.
 *
 * - The new visit (#visita/<id>): one screen with what is due already
 *   computed («Le toca pagar $ X»), «Entregué todo / Devolvió algo / No se
 *   entregó», the receipt code optional (never blocks: without it the visit
 *   goes «sin código»), «¿Cómo pagó?» (Efectivo, Transferencia, Mixto,
 *   Parcial, Ya estaba pagado, No pagó) and, with a transfer, «Sacar foto» or
 *   «Lo manda después · queda a mi cargo». It fills app3.js's draft and saves
 *   with app3.js's storeVisit: the same payload and the same outbox.
 * - «Mis comprobantes a reclamar» (#reclamar, a card on the home screens):
 *   the driver's own pending receipts, unpaid orders and charges, from
 *   /reparto/api/home (kept on the phone for no signal), with «Sacar foto»
 *   (a «receipt» event of that route, in the same outbox) and «Pedir por
 *   WhatsApp». They close by themselves on the server when the money
 *   arrives.
 * - The reminder of a route of an earlier day still open: a gentle notice,
 *   «Ahora no» hides it for the day. Never a block.
 * - #4 «Cambiar el orden» (#orden): the driver's own order, kept on the
 *   phone (local.order) and sent to /reparto/api/order when there is signal.
 *
 * 19.0.1.129.33 («Etapa 3b»), still only with the flag on:
 * - A tap the visit screen cannot take (the screen on display is older than
 *   the route's state: the route was closed on this phone, the order was
 *   held after a download, «Llegué» comes first...) says why in a toast and
 *   redraws the screen, instead of doing nothing. A closed route then shows
 *   «Ruta cerrada» with app3.js's «Reabrir la ruta» (129.25) when it applies.
 * - The customer's WhatsApp on «Ubicación y horarios» (#cliente/<id>, hook
 *   "customerHtml"): Confirmar / Corregir / Cargar, saved with app3.js's
 *   saveWhatsapp, the same «whatsapp» sync event and server validation as
 *   the old visit (models/reparto_sync.py _apply_whatsapp).
 */
(function () {
  "use strict";

  var APP = window.RepartoApp;
  if (!APP) return;

  var API_HOME = "/reparto/api/home";
  var API_ORDER = "/reparto/api/order";
  var HOME_EVERY_MS = 120000;
  var RECEIPT_INPUT_ACCEPT = "image/*,application/pdf,.pdf";
  var PAY_LABELS = { cash: "Efectivo", transfer: "Transferencia", mixed: "Mixto", partial: "Parcial",
                     already: "Ya estaba pagado", none: "No pagó" };

  var home = null;        // the last /reparto/api/home answer with v2
  var homeLoadedFor = null;
  var lastHome = 0, fetchingHome = false;
  var remindOff = "";     // the day «Ahora no» hid the reminder
  var sentPhotos = {};    // stop id -> a receipt photo queued from #reclamar
  var orderTimer = null;
  var waEdit = null;      // 19.0.1.129.33: {stop_id, number} while the customer's WhatsApp is typed

  function h(v) { return APP.h(v); }
  function money(n) { return APP.money(n); }
  function round2(n) { return APP.round2(n); }
  function route() { return APP.route(); }
  function local() { return APP.local(); }
  function on() { var r = route(); return !!(r && r.reparto_v2); }
  function homeOn() { return on() || !!(home && home.v2); }
  function today() {
    var d = new Date();
    return d.getFullYear() + "-" + (d.getMonth() + 1) + "-" + d.getDate();
  }

  // ------------------------------------------------------------ the visit

  function dueBase(stop) {
    if (typeof stop.v2_due === "number") return stop.v2_due;
    if (stop.payments_paid) return 0;
    if (typeof stop.payments_due === "number") return Math.min(stop.payments_due, stop.order_amount || 0);
    return stop.order_amount || 0;
  }
  // «Le toca pagar»: what the order still owes, minus what he returns now
  function dueOf(stop, d) {
    return Math.max(round2(dueBase(stop) - APP.returnsAmount(stop, d ? d.returns : [])), 0);
  }
  function shortOf(stop, d) {
    var paid = (d.cash || 0) + (d.transfer === "yes" ? d.transfer_amount || 0 : 0);
    return round2(dueOf(stop, d) - paid);
  }

  function usable(stop) {
    var l = local();
    return on() && !!stop && !stop.parcel && !stop.retained && !APP.redOf(stop) && !!l && !l.closed &&
      !APP.needsArrival(stop);
  }

  // 19.0.1.129.33: why a tap on the visit screen cannot be taken, "" when it
  // can. The screen on display may be older than the route's state.
  function closedText(what) {
    return APP.canReopen && APP.canReopen() ? "La ruta está cerrada: reabrila para corregir " + what + "." :
      // 25/11/2026: drivers no longer reopen; administración or caja does
      "La ruta está cerrada. Para corregir " + what + ", pedile a administración o caja que la reabra.";
  }
  function blockedReason(stop) {
    var l = local();
    if (!stop) return "Esta visita ya no está en la ruta.";
    if (!l || l.closed) return closedText("esta visita");
    if (!on()) return "La visita cambió de pantalla: revisala de nuevo.";
    if (stop.retained) return "Este pedido quedó retenido: no se entrega. Volvelo al depósito.";
    if (stop.parcel) return "Es una encomienda: se carga en su propia pantalla.";
    if (APP.redOf(stop)) return "Cliente en rojo: seguí los pasos de la pantalla.";
    if (APP.needsArrival(stop)) return "Primero tocá «Llegué».";
    return "";
  }

  function modeOf(stop, v) {
    if (!v || v.outcome !== "delivered") return null;
    if (v.already_paid) return "already";
    if (v.no_payment) return "none";
    var transfer = v.transfer === "yes" ? v.transfer_amount || 0 : 0;
    if ((v.cash || 0) + transfer + 1 < dueOf(stop, v)) return "partial";
    if (v.cash > 0 && transfer > 0) return "mixed";
    if (transfer > 0) return "transfer";
    if (v.cash > 0) return "cash";
    return null;
  }

  function draftFor(stop) {
    var d = APP.getDraft();
    if (d && d.stop_id === stop.id && d.v2) return d;
    d = APP.newDraft(stop);
    var v = APP.visitOf(stop);
    d.adding = !d.returns.length;
    d.v2 = true;
    d.pay = v ? modeOf(stop, v) : null;
    d.auto = false;
    if (!v && stop.payments_paid) { d.pay = "already"; d.already_paid = true; }
    APP.setDraft(d);
    return d;
  }

  // the amounts the chosen way of paying fills in (editable)
  function refill(stop, d) {
    var due = dueOf(stop, d);
    if (!d.auto) return;
    if (d.pay === "cash") d.cash = due;
    else if (d.pay === "transfer") d.transfer_amount = due;
    else if (d.pay === "mixed") d.transfer_amount = Math.max(round2(due - (d.cash || 0)), 0);
  }

  function setPay(stop, d, mode) {
    var due = dueOf(stop, d);
    d.pay = mode;
    d.already_paid = false; d.to_confirm = false; d.no_payment = false;
    d.auto = mode === "cash" || mode === "transfer" || mode === "mixed";
    if (mode === "cash") {
      d.transfer = "no"; d.transfer_amount = 0; d.receipt_answer = null; d.cash = due;
    } else if (mode === "transfer") {
      d.cash = 0; d.transfer = "yes"; d.transfer_amount = due;
    } else if (mode === "mixed") {
      if (!(d.cash > 0 && d.cash < due)) d.cash = 0;
      d.transfer = "yes"; d.transfer_amount = Math.max(round2(due - d.cash), 0);
    } else if (mode === "partial") {
      d.cash = 0; d.transfer = "no"; d.transfer_amount = 0; d.receipt_answer = null;
    } else {
      d.cash = 0; d.transfer = "no"; d.transfer_amount = 0; d.receipt_answer = null;
      if (mode === "already") {
        d.already_paid = true;
        d.to_confirm = !stop.payments_paid && !!stop.payments_to_confirm;
      } else if (mode === "none") d.no_payment = true;
    }
  }

  function shortHtml(stop, d) {
    if (!d.pay || d.pay === "already" || d.pay === "none") return "";
    var short = shortOf(stop, d);
    if (short <= 1) return "";
    return '<div class="small red-text v2short">Faltan ' + money(short) + ": " +
      (stop.v2_exempt ? "quedan a cuenta del cliente (tiene cuenta corriente). Se avisa al vendedor."
        : "quedan a tu cargo y se avisa al vendedor.") + "</div>";
  }

  function btn(action, value, label, onState, extra) {
    return '<button type="button" class="opt' + (extra ? " " + extra : "") + (onState ? " on" : "") +
      '" data-a5="' + action + '" data-v="' + value + '">' + label + "</button>";
  }

  function returnsHtml(stop, d) {
    var lines = stop.lines, out = "";
    d.returns.forEach(function (r, i) {
      var line = lines.filter(function (l) { return l.id === r.line_id; })[0] || {};
      out += '<div class="ret"><div class="split"><span><b>' + APP.qty(r.qty) + "</b> · " + h(line.product) +
        "</span><b>− " + money((line.unit_price || 0) * r.qty) + '</b></div><div class="split small muted"><span>Motivo: ' +
        h(r.reason) + '</span><button class="link red" data-a="ret-del" data-i="' + i + '">Quitar</button></div></div>';
    });
    if (d.adding) {
      out += '<div class="ret"><select class="inp" data-f="ret-line"><option value="">Elegí el producto</option>' +
        lines.map(function (l) {
          return '<option value="' + l.id + '"' + (d.retLine === l.id ? " selected" : "") + ">" + h(l.product) +
            " (" + APP.qty(l.qty) + ")</option>";
        }).join("") + "</select>" +
        '<div class="grid2"><input class="inp" data-f="ret-qty" inputmode="decimal" placeholder="Cantidad" value="' +
        h(d.retQty || "") + '"><input class="inp" data-f="ret-reason" list="motivos5" placeholder="Motivo" value="' +
        h(d.retReason || "") + '"></div><datalist id="motivos5">' +
        APP.reasons.map(function (r) { return '<option value="' + h(r) + '">'; }).join("") + "</datalist>" +
        '<button class="opt second" data-a="ret-add">Agregar devolución</button></div>';
    } else {
      out += '<button class="link" data-a="ret-more">+ Otro producto devuelto</button>';
    }
    return out;
  }

  function receiptHtml(stop, d) {
    var ra = d.receipt_answer, hasPhoto = !!(d.thumb || d.receipt_photo);
    var input = '<input type="file" accept="' + RECEIPT_INPUT_ACCEPT + '" data-f="rphoto" hidden>';
    var out = '<div class="label">Comprobante</div><div class="grid2">' +
      (hasPhoto ? '<button class="opt teal photo-btn' + (ra === "photo" ? " on" : "") + '" data-a="rcpt" data-v="photo">' +
        APP.icons.camera + "Foto lista</button>"
        : '<label class="opt teal photo-btn' + (ra === "photo" ? " on" : "") + '">' + APP.icons.camera +
        "Sacar foto" + input + "</label>") +
      '<button class="opt' + (ra === "not_yet" ? " on" : "") + '" data-a="rcpt" data-v="not_yet">Lo manda después · ' +
      "queda a mi cargo</button></div>";
    if (ra === "photo" && d.thumb) {
      out += '<div class="photo"><img src="' + d.thumb + '" alt="Foto del comprobante">' +
        '<label class="opt second">Cambiar foto' + input + "</label></div>";
    } else if (ra === "not_yet") {
      out += '<div class="small muted">Va a «Mis comprobantes a reclamar» y se cierra solo cuando llega a la empresa. ' +
        "Reclamarlo es tu responsabilidad.</div>";
    } else if (ra === "customer_wa" || ra === "forwarded") {
      out += '<div class="small muted">Respuesta anterior: el cliente lo manda. Queda pendiente a tu nombre.</div>';
    }
    return out;
  }

  function amountField(field, label, value) {
    return '<label class="field">' + label + '<input class="inp money" data-f="' + field +
      '" inputmode="decimal" placeholder="$ 0" value="' + (value ? h(money(value)) : "") + '"></label>';
  }

  function paymentHtml(stop, d) {
    var p = d.pay, out = "";
    if (p === "cash") out += amountField("cash", "Efectivo", d.cash);
    else if (p === "transfer") out += amountField("tamount", "Transferencia", d.transfer_amount);
    else if (p === "mixed") {
      out += amountField("cash", "Efectivo", d.cash) + amountField("tamount", "Transferencia", d.transfer_amount) +
        '<div class="small muted">La transferencia completa el resto sola.</div>';
    } else if (p === "partial") {
      out += amountField("cash", "Efectivo", d.cash);
      if (d.transfer === "yes") out += amountField("tamount", "Transferencia", d.transfer_amount);
      out += '<button type="button" class="link" data-a5="ptr">' +
        (d.transfer === "yes" ? "Sin transferencia" : "+ Una parte por transferencia") + "</button>";
    } else if (p === "already") {
      if (stop.payments_paid) {
        out += '<div class="small pay-paid"><b>Ya está pagado</b>' +
          (stop.payments_paid_how ? " (" + h(stop.payments_paid_how) + ")" : "") + ". No cobres nada.</div>";
      } else if (stop.payments_to_confirm) {
        out += '<div class="small pay-confirm">El cliente cargó un comprobante de ' +
          money(stop.payments_to_confirm_amount || 0) + " que cubre lo que debe: queda «a confirmar».</div>";
      } else {
        out += '<div class="small pay-confirm">El sistema todavía ve ' + money(dueBase(stop)) + " sin pagar. " +
          "Se guarda igual y administración lo revisa. No queda a tu nombre.</div>";
      }
    } else if (p === "none") {
      out += '<div class="small pay-confirm">El pedido (' + money(dueOf(stop, d)) + ") queda pendiente a tu " +
        "nombre hasta que se cobre: va a «Mis comprobantes a reclamar».</div>";
    }
    out += '<div id="v2-short">' + shortHtml(stop, d) + "</div>";
    if (d.transfer === "yes" && p !== "already" && p !== "none") out += receiptHtml(stop, d);
    return out;
  }

  function visitHtml(stop) {
    var d = draftFor(stop);
    refill(stop, d);
    var due = dueOf(stop, d), paid = !!stop.payments_paid;
    var nd = d.outcome === "not_delivered";
    var ret = APP.returnsAmount(stop, d.returns);
    var out = '<div class="head"><a class="back" href="#visitas">‹ Visitas</a><h1>' + h(stop.name) + "</h1>" +
      '<div class="sub">' + h([stop.order, (stop.parcels || 0) + (stop.parcels === 1 ? " bulto" : " bultos"),
        stop.address].filter(Boolean).join(" · ")) + "</div></div>" +
      '<div id="status">' + APP.statusBanner() + "</div>" + APP.packedHtml(stop) +
      '<a class="place" href="#cliente/' + stop.id + '">' + APP.icons.pin +
      '<span class="txt"><b>Ubicación y horarios</b><span>' +
      h(APP.daysHoursText(APP.customerOf(stop)) || "Ver o cargar") + '</span></span><span class="chev">›</span></a>' +
      APP.accountLink(stop) + APP.noticeHtml(stop, "place-notice") + '<div class="body">';

    out += '<div class="card v2due' + (paid ? " paid" : "") + '"><div class="small muted">' +
      (paid ? "Ya está pagado" : "Le toca pagar") + '</div><div class="v2amount">' + money(paid ? 0 : due) + "</div>" +
      '<div class="small muted">Pedido ' + money(dueBase(stop)) + (ret ? " · devolución − " + money(ret) : "") + "</div></div>";

    out += '<div class="label">Entrega</div><div class="grid3">' +
      btn("deliv", "all", "Entregué todo", !nd && d.returns_answer === "none") +
      btn("deliv", "ret", "Devolvió algo", !nd && d.returns_answer === "yes") +
      btn("deliv", "nd", "No se entregó", nd, "amberopt") + "</div>";

    if (nd) {
      out += '<div class="card"><label class="stack">¿Por qué no se entregó? (obligatorio al cerrar la ruta)' +
        '<input class="inp" data-f="reason" value="' + h(d.reason) + '" placeholder="Ej.: estaba cerrado"></label>' +
        '<div class="small muted">Los ' + (stop.parcels || 0) + " bultos vuelven al depósito.</div></div></div>" +
        '<div class="foot"><button class="btn" data-a="save-nd">Guardar: no se entregó</button></div>';
      return out;
    }
    if (d.returns_answer === "yes") out += '<div class="card">' + returnsHtml(stop, d) + "</div>";

    if (stop.code_hash && !stop.code_locked) {
      out += '<label class="field small">Código de recibido (opcional)<input class="inp code" data-f="code" ' +
        'inputmode="numeric" maxlength="4" autocomplete="off" value="' + h(d.code) + '"></label>' +
        '<div id="code-status">' + (d.code ? APP.codeStatus(stop) : "") + "</div>";
    }

    out += '<div class="label">¿Cómo pagó?</div><div class="grid3">' +
      btn("pay", "cash", "Efectivo", d.pay === "cash") +
      btn("pay", "transfer", "Transferencia", d.pay === "transfer") +
      btn("pay", "mixed", "Mixto", d.pay === "mixed") +
      btn("pay", "partial", "Parcial", d.pay === "partial") +
      btn("pay", "already", "Ya estaba pagado", d.pay === "already", "teal") +
      btn("pay", "none", "No pagó", d.pay === "none") + "</div>" +
      '<div class="card v2pay">' + paymentHtml(stop, d) + "</div>";

    out += '<label class="stack">Comentario (opcional)<input class="inp" data-f="comment" value="' + h(d.comment) +
      '" placeholder="Ej.: transfirió desde la cuenta del hijo"></label></div>' +
      '<div class="foot"><button class="btn" data-a5="save">Guardar visita</button></div>';
    return out;
  }

  function payText(stop, d) {
    if (d.pay === "already") return d.to_confirm ? "ya estaba pagado, a confirmar" :
      (stop.payments_paid ? "ya estaba pagado" : "ya estaba pagado (administración lo revisa)");
    if (d.pay === "none") return "no pagó: " + money(dueOf(stop, d)) + " quedan pendientes a tu nombre";
    var bits = [];
    if (d.cash > 0) bits.push("efectivo " + money(d.cash));
    if (d.transfer === "yes") {
      bits.push("transferencia " + money(d.transfer_amount) + (d.receipt_answer === "photo" ? " con foto" :
        " · comprobante a reclamar"));
    }
    return bits.join(" + ");
  }

  function save(stop) {
    var d = draftFor(stop);
    if (d.outcome === "not_delivered") return APP.actions["save-nd"](null, stop);
    var err = "";
    if (!APP.returnsAnswered(d)) err = d.returns_answer === "yes" ? "Cargá lo que devolvió." : "Tocá «Entregué todo» o «Devolvió algo».";
    else if (!d.pay) err = "Elegí cómo pagó.";
    if (!err && d.pay !== "already" && d.pay !== "none") {
      if (d.pay === "cash" || d.transfer !== "yes") { d.transfer = "no"; d.transfer_amount = 0; d.receipt_answer = null; }
      if (d.pay === "transfer") d.cash = 0;
      if (isNaN(d.cash) || isNaN(d.transfer_amount)) err = "El importe no es un número.";
      else if (!((d.cash || 0) + (d.transfer_amount || 0) > 0)) err = "Poné cuánto pagó.";
      else if (d.pay === "mixed" && !(d.cash > 0 && d.transfer_amount > 0)) err = "En «Mixto» van efectivo y transferencia.";
      else if (d.transfer === "yes" && !(d.transfer_amount > 0)) err = "Poné el importe transferido.";
      else if (d.transfer === "yes" && !d.receipt_answer) err = "Comprobante: sacá la foto o tocá «Lo manda después».";
      else if (d.transfer === "yes" && d.receipt_answer === "photo" && !d.photo && !d.thumb && !d.receipt_photo) {
        err = "Sacá la foto del comprobante.";
      }
    }
    if (err) return APP.toast(err, true);
    // the code is optional: without a right one the visit goes «sin código»
    if (!d.code_ok) { d.code = ""; d.without_code = true; } else d.without_code = false;
    if (d.pay === "already" || d.pay === "none") setPay(stop, d, d.pay);
    else { d.already_paid = false; d.to_confirm = false; d.no_payment = false; }
    var short = d.pay === "already" || d.pay === "none" ? 0 : shortOf(stop, d);
    var text = stop.name + ": " + (d.returns_answer === "yes" ? "devolvió " + money(APP.returnsAmount(stop, d.returns)) :
      "entregó todo") + ", " + payText(stop, d) + "." +
      (short > 1 ? " Faltan " + money(short) + (stop.v2_exempt ? ": quedan a cuenta del cliente." : ": quedan a tu cargo.") : "");
    APP.askConfirm({
      title: "¿Guardar la visita?", ok: "Sí, guardar", text: text,
      onOk: function () {
        var cwa = APP.whatsappOf(stop);
        APP.withGps("close_visit", "guardar la visita", function (gps) {
          if (d.transfer === "yes" && d.receipt_answer === "not_yet" && cwa.number) {
            window.open(APP.waLink(APP.waDigits(cwa.number), APP.visitClaim(stop, d.transfer_amount)), "_blank", "noopener");
          }
          APP.storeVisit(stop, d, gps).then(function () { APP.toast("Visita guardada en el celular."); });
          APP.go("#visitas");
        });
      },
    });
  }

  // ------------------------------- 19.0.1.129.33: the customer's WhatsApp

  // on «Ubicación y horarios» (app3.js viewCustomer, hook "customerHtml")
  function customerHtml(stop) {
    var l = local();
    if (!on() || !stop || !l || l.closed) return "";
    var wa = APP.whatsappOf(stop), editing = !!(waEdit && waEdit.stop_id === stop.id);
    var out = '<div class="label">WhatsApp del cliente</div><div class="card v2wa"><div class="stack">';
    if (editing) {
      out += '<input class="inp" data-f5="cwa" inputmode="tel" value="' + h(waEdit.number) +
        '" placeholder="Ej.: 299 000-0000"></div><div class="grid2"><button class="opt primary" data-a5="cwa-save">' +
        'Guardar número</button><button class="opt second" data-a5="cwa-cancel">Cancelar</button></div>';
    } else if (wa.number) {
      out += '<span class="phone">' + h(wa.number) + "</span>" +
        (wa.confirmed ? '<span class="okline">' + APP.icons.check + "Confirmado</span>" : "") + "</div>" +
        '<div class="grid2"><button class="opt primary" data-a5="cwa-ok">Confirmar</button>' +
        '<button class="opt second" data-a5="cwa-edit">Corregir</button></div>';
    } else {
      out += '<span class="muted">El contacto no tiene número.</span></div>' +
        '<button class="opt primary" data-a5="cwa-edit">Cargar</button>';
    }
    return out + '<div class="small muted">Se guarda en el celular y se envía al sincronizar.</div></div>';
  }

  function customerWa(a, stop) {
    var l = local();
    if (!stop || !on() || !l || l.closed) {
      APP.toast(!stop ? "Este cliente ya no está en la ruta." : closedText("los datos del cliente"), true);
      return APP.rerender();
    }
    if (a === "cwa-edit") waEdit = { stop_id: stop.id, number: APP.whatsappOf(stop).number || "" };
    else if (a === "cwa-cancel") waEdit = null;
    else if (a === "cwa-ok") return APP.saveWhatsapp(stop, APP.whatsappOf(stop).number, "confirm");
    else if (a === "cwa-save") {
      var number = String(waEdit && waEdit.stop_id === stop.id ? waEdit.number : "").trim();
      if (number.replace(/\D/g, "").length < 8) return APP.toast("El número es muy corto.", true);
      var had = APP.whatsappOf(stop).number;
      waEdit = null;
      return APP.saveWhatsapp(stop, number, had ? "correct" : "load");
    }
    APP.rerender();
  }

  // ------------------------------------------------- «Mis comprobantes»

  function daysAgo(value) {
    if (!value) return "";
    var t = Date.parse(String(value).replace(" ", "T") + (/[zZ]|[+-]\d\d:?\d\d$/.test(value) ? "" : "Z"));
    if (isNaN(t)) return "";
    var days = Math.floor((Date.now() - t) / 86400000);
    return days <= 0 ? "hoy" : days === 1 ? "ayer" : "hace " + days + " días";
  }

  function owedItems() {
    return ((home && home.receipts) || []).filter(function (r) { return r.state === "open" || r.state === "overdue"; });
  }
  function charges() { return (home && home.charges) || []; }

  function reminderHtml() {
    var stale = (home && home.stale_routes) || [];
    if (!stale.length || remindOff === today()) return "";
    var open = APP.otherRoutes(), r = stale[0], cur = route();
    var canOpen = open.some(function (x) { return x.id === r.id; }) && !(cur && cur.id === r.id);
    return '<div class="banner warn v2remind"><span>Tenés la ruta del ' + h(r.date) + " sin cerrar" +
      (stale.length > 1 ? " (y " + (stale.length - 1) + " más)" : "") + ". Cerrala cuando puedas.</span>" +
      (canOpen ? ' <button class="link" data-a="open-route" data-id="' + r.id + '">Abrirla</button>' : "") +
      ' <button class="link" data-a5="remind-off">Ahora no</button></div>';
  }

  function homeHtml() {
    if (!homeOn() || !home) return "";
    var n = owedItems().length + charges().length;
    return reminderHtml() + '<a class="card v2home" href="#reclamar"><div class="split"><b>Mis comprobantes a reclamar</b>' +
      '<span class="pill ' + (n ? "pendpill" : "paid") + '">' + n + "</span></div>" +
      '<div class="small muted">' + (n ? "Tocá para verlos" : "Nada pendiente") +
      "</div></a>";
  }

  function claimRow(r) {
    var late = r.state === "overdue", done = r.state === "fulfilled";
    var queued = sentPhotos[r.stop_id];
    var pill = done ? '<span class="pill paid">Llegó</span>' :
      queued ? '<span class="pill pendpill">Foto guardada en el celular · se envía sola</span>' :
      late ? '<span class="pill not_paid">Vencida desde ' + h(r.overdue_since) + "</span>" :
      '<span class="pill pendpill">Pendiente</span>';
    var out = '<div class="card rcpt' + (late ? " late" : "") + (done ? " done" : "") + '" style="gap:6px">' +
      '<div class="split"><b>' + h(r.name) + '</b><b class="' + (done ? "strike" : "") + '">' + money(r.amount) + "</b></div>" +
      '<div class="small"><b>' + h(APP.kindLabel(r.kind) || "") + "</b> · " + h(r.document || r.order) + " · " +
      h(daysAgo(r.delivered_at)) + "</div>" + pill;
    if (!done) {
      out += '<div class="grid2">' + (r.route_id ? '<label class="opt teal photo-btn">' + APP.icons.camera + "Sacar foto" +
        '<input type="file" accept="' + RECEIPT_INPUT_ACCEPT + '" data-f5="photo" data-stop="' + r.stop_id +
        '" data-route="' + r.route_id + '" data-amount="' + (r.amount || 0) + '" hidden></label>' : "") +
        (r.whatsapp ? '<a class="opt second" target="_blank" rel="noopener" href="' +
          h(APP.waLink(r.whatsapp, r.claim || "")) + '">Pedir por WhatsApp</a>' : "") + "</div>";
    }
    return out + "</div>";
  }

  function claimsHtml() {
    var cur = route();
    var back = cur ? (local() && local().departed ? "#visitas" : "#") : "#";
    var out = '<div class="head"><a class="back" href="' + back + '">‹ Volver</a><h1>Mis comprobantes a reclamar</h1>' +
      '<div class="sub">' + h(APP.user()) + "</div></div>" +
      '<div id="status">' + APP.statusBanner() + "</div>" + reminderHtml() + '<div class="body">';
    if (!home) {
      return out + '<div class="card small muted">Todavía no se descargó la lista. Abrí la app con señal.</div></div>';
    }
    var owed = owedItems(), list = charges();
    var resolved = (home.receipts || []).filter(function (r) { return r.state === "fulfilled"; });
    if (!owed.length && !list.length) {
      out += '<div class="card"><b>Nada pendiente.</b><div class="small muted">No tenés comprobantes para reclamar.</div></div>';
    }
    owed.forEach(function (r) { out += claimRow(r); });
    if (list.length) {
      out += '<div class="label">Otros pendientes</div>';
      list.forEach(function (c) {
        out += '<div class="card rcpt late" style="gap:6px"><div class="split"><b>' + h(c.name || c.label) + "</b><b>" +
          money(c.amount) + '</b></div><div class="small"><b>' + h(c.label) + "</b>" +
          (c.order ? " · " + h(c.order) : "") + (c.date ? " · " + h(c.date) : "") + "</div>" +
          (c.text ? '<div class="small muted">' + h(c.text) + "</div>" : "") + "</div>";
      });
    }
    if (resolved.length) {
      out += '<div class="label">Resueltas hoy</div>';
      resolved.forEach(function (r) { out += claimRow(r); });
    }
    out += '<div class="note">Te pedimos que nos ayudes a reclamarle a cada cliente su comprobante. ' +
      "Cada uno se cierra solo cuando el comprobante o el pago llega a la empresa, aunque llegue tarde.</div></div>";
    return out;
  }

  function sendPhoto(input) {
    var file = input.files && input.files[0];
    if (!file) return;
    var stopId = +input.getAttribute("data-stop"), routeId = +input.getAttribute("data-route");
    var amount = Number(input.getAttribute("data-amount")) || 0;
    APP.toast("Preparando el comprobante…");
    APP.readReceipt(file).then(function (res) {
      var payload = { photo_uuid: APP.uuid(), stop_id: stopId, visit_uuid: "", transfer_amount: amount,
                      photo: res.photo };
      sentPhotos[stopId] = true;
      return APP.enqueueRoute(routeId, "receipt", payload, "receipt5:" + stopId);
    }).then(function () {
      APP.toast("Comprobante guardado en el celular: se envía solo.");
      APP.rerender();
    }).catch(function (e) { APP.toast((e && e.message) || "No se pudo leer la foto.", true); });
  }

  function loadCachedHome() {
    var uid = APP.uid();
    if (uid === null || uid === undefined || homeLoadedFor === uid) return Promise.resolve();
    homeLoadedFor = uid;
    return Promise.all([APP.kvGet(APP.userKey("home")), APP.kvGet(APP.userKey("remindOff"))]).then(function (res) {
      if (APP.uid() !== uid) return;
      if (!home && res[0] && res[0].v2) home = res[0];
      remindOff = res[1] || "";
    }).catch(function () {});
  }

  function onHomeScreen() {
    var part = location.hash.replace(/^#/, "").split("/")[0];
    return part === "" || part === "visitas" || part === "reclamar" || part === "carga";
  }
  function typing() {
    var el = document.activeElement;
    return !!el && /^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName || "");
  }

  function fetchHome(force) {
    var now = Date.now();
    if (fetchingHome || (!force && now - lastHome < HOME_EVERY_MS - 500)) return Promise.resolve();
    if (APP.uid() === null || APP.uid() === undefined) return Promise.resolve();
    fetchingHome = true; lastHome = now;
    var uid = APP.uid();
    return APP.fetchWithTimeout(API_HOME, { credentials: "same-origin", cache: "no-store" }, 15000)
      .then(function (res) {
        if (APP.isLoginPage(res)) throw new Error("auth");
        return res.json();
      }).then(function (data) {
        if (!data || !data.ok || APP.uid() !== uid) return;
        if (data.user_id && data.user_id !== uid) return;
        var before = JSON.stringify(home);
        home = data.v2 ? data : null;
        if (data.v2) sentPhotos = {};
        APP.kvSet(APP.userKey("home"), home);
        if (JSON.stringify(home) !== before && onHomeScreen() && !typing()) APP.rerender();
      }).catch(function () {})
      .then(function () { fetchingHome = false; });
  }

  // ------------------------------------------------------- #4 the order

  function applyOrder(r, order) {
    if (!r || !order || !order.length) return;
    var pos = {};
    order.forEach(function (id, i) { pos[id] = i; });
    var rank = function (s) { return pos[s.id] === undefined ? 100000 + (r.stops.indexOf(s)) : pos[s.id]; };
    var ranked = r.stops.map(function (s) { return { s: s, k: rank(s) }; });
    ranked.sort(function (a, b) { return a.k - b.k; });
    r.stops = ranked.map(function (x) { return x.s; });
  }

  function postOrder() {
    var r = route(), l = local();
    if (!r || !l || !l.order || !l.orderPending || l.routeId !== r.id) return;
    var routeId = r.id;
    APP.fetchWithTimeout(API_ORDER, { method: "POST", credentials: "same-origin", cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ route_id: routeId, stop_ids: l.order, user_id: APP.uid() }) }, 15000)
      .then(function (res) {
        if (APP.isLoginPage(res)) throw new Error("auth");
        // 404: the flag went off on the server; the order stays on this phone
        if (res.ok || res.status === 404 || res.status === 409) {
          var cur = local();
          if (cur && cur.routeId === routeId) { cur.orderPending = false; APP.saveLocal(); }
        }
      }).catch(function () {});
  }

  function move(id, delta) {
    var r = route(), l = local();
    if (!r || !l) return;
    var i = r.stops.map(function (s) { return s.id; }).indexOf(id), j = i + delta;
    if (i < 0 || j < 0 || j >= r.stops.length) return;
    var s = r.stops[i];
    r.stops[i] = r.stops[j]; r.stops[j] = s;
    l.order = r.stops.map(function (x) { return x.id; });
    l.orderPending = true;
    APP.saveRoute();
    APP.saveLocal();
    clearTimeout(orderTimer);
    orderTimer = setTimeout(postOrder, 1500);
    APP.rerender();
  }

  function orderHtml() {
    var list = APP.stops();
    var rows = list.map(function (s, i) {
      var st = APP.stopState(s);
      return '<div class="visit' + (st !== "pending" ? " done" : "") + '"><div class="split"><span class="t">' + (i + 1) +
        " · " + h(s.name) + '</span><span class="v2move">' +
        '<button class="opt" data-a5="up" data-id="' + s.id + '"' + (i === 0 ? " disabled" : "") + ' aria-label="Subir">↑</button>' +
        '<button class="opt" data-a5="down" data-id="' + s.id + '"' + (i === list.length - 1 ? " disabled" : "") +
        ' aria-label="Bajar">↓</button></span></div><div class="d">' + h(s.address || "") + "</div></div>";
    }).join("");
    return '<div class="head"><a class="back" href="#visitas">‹ Visitas</a><h1>Cambiar el orden</h1>' +
      '<div class="sub">Subí o bajá cada cliente. La sugerida es la primera sin visitar.</div></div>' +
      '<div id="status">' + APP.statusBanner() + "</div>" + '<div class="body">' + rows + "</div>" +
      '<div class="foot"><a class="btn" href="#visitas">Listo</a></div>';
  }

  // ------------------------------------------------------------ hooks

  function view(part, id) {
    if (part === "reclamar" && homeOn()) return claimsHtml();
    var r = route(), l = local();
    if (!r || !l || l.closed) return null;
    if (part === "orden" && on() && l.departed) return orderHtml();
    if (part === "visita") {
      var stop = APP.stopById(id);
      if (usable(stop)) return visitHtml(stop);
    }
    return null;
  }

  function visitsTools() {
    var l = local();
    return on() && l && l.departed && !l.closed ? ' · <a class="link" href="#orden">Cambiar el orden</a>' : "";
  }

  function routeApplied() {
    var r = route(), l = local();
    if (r && l && l.routeId === r.id && l.order) applyOrder(r, l.order);
    loadCachedHome().then(function () {
      fetchHome(false);
      if (l && l.orderPending) postOrder();
    });
  }

  // ------------------------------------------------------------ events

  document.addEventListener("click", function (ev) {
    var el = ev.target.closest && ev.target.closest("[data-a5]");
    if (!el || el.disabled) return;
    ev.preventDefault();
    var a = el.getAttribute("data-a5"), v = el.getAttribute("data-v");
    var stop = APP.stopById(location.hash.replace(/^#/, "").split("/")[1]);
    if (a === "remind-off") {
      remindOff = today();
      if (APP.uid() !== null) APP.kvSet(APP.userKey("remindOff"), remindOff);
      return APP.rerender();
    }
    if (a === "up" || a === "down") return move(+el.getAttribute("data-id"), a === "up" ? -1 : 1);
    if (a.indexOf("cwa-") === 0) return customerWa(a, stop);
    // 19.0.1.129.33: say why instead of doing nothing, and show the real state
    if (!stop || !usable(stop)) {
      APP.toast(blockedReason(stop) || "Esta visita no se puede cambiar ahora.", true);
      return APP.rerender();
    }
    var d = draftFor(stop);
    if (a === "deliv") {
      if (v === "nd") d.outcome = "not_delivered";
      else {
        d.outcome = "delivered";
        if (v === "all") { d.returns_answer = "none"; d.returns = []; d.adding = true; }
        else { d.returns_answer = "yes"; d.adding = !d.returns.length; }
      }
    } else if (a === "pay") setPay(stop, d, v);
    else if (a === "ptr") {
      if (d.transfer === "yes") { d.transfer = "no"; d.transfer_amount = 0; d.receipt_answer = null; }
      else { d.transfer = "yes"; d.transfer_amount = 0; }
    } else if (a === "save") return save(stop);
    APP.rerender();
  });

  document.addEventListener("input", function (ev) {
    var el = ev.target, f = el.getAttribute && el.getAttribute("data-f");
    // 19.0.1.129.33: the customer's WhatsApp being typed (data-f5: app3.js
    // reads data-f="wa" into the visit draft, which this screen does not have)
    if (el.getAttribute && el.getAttribute("data-f5") === "cwa") {
      if (waEdit) waEdit.number = el.value;
      return;
    }
    if (f !== "cash" && f !== "tamount") return;
    var stop = APP.stopById(location.hash.replace(/^#/, "").split("/")[1]);
    var d = APP.getDraft();
    if (!stop || !d || !d.v2 || d.stop_id !== stop.id) return;
    // app3.js already read the amount into the draft
    if (d.pay === "mixed" && f === "cash") {
      d.transfer_amount = Math.max(round2(dueOf(stop, d) - (d.cash || 0)), 0);
      var t = document.querySelector('[data-f="tamount"]');
      if (t) t.value = d.transfer_amount ? money(d.transfer_amount) : "";
    } else d.auto = false;
    var box = document.getElementById("v2-short");
    if (box) box.innerHTML = shortHtml(stop, d);
  });

  document.addEventListener("change", function (ev) {
    var el = ev.target;
    if (el && el.getAttribute && el.getAttribute("data-f5") === "photo") sendPhoto(el);
  });

  setInterval(function () {
    loadCachedHome();
    if (document.visibilityState === "visible" && onHomeScreen()) fetchHome(false);
  }, 30000);
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") fetchHome(false);
  });

  // 19.0.1.129.33: a WhatsApp half typed does not follow to another screen
  window.addEventListener("hashchange", function () { waEdit = null; });

  var ext = window.RepartoExt || {};
  ext.view = view;
  ext.customerHtml = customerHtml;
  ext.homeHtml = homeHtml;
  ext.visitsTools = visitsTools;
  ext.routeApplied = routeApplied;
  window.RepartoExt = ext;
  // for tests (tests/data/reparto_v129_28.js)
  window.RepartoV2 = { dueOf: dueOf, shortOf: shortOf, home: function () { return home; },
                       blockedReason: blockedReason,
                       fetchHome: fetchHome, postOrder: postOrder, save: save, setHome: function (x) { home = x; } };
  if (route()) { routeApplied(); APP.rerender(); }
})();
