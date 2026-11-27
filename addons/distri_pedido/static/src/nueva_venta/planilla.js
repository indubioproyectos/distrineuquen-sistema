/* «Nueva venta» on the PC, «estilo planilla» (19.0.1.129.53,
 * controllers/nueva_venta_pc.py). Keyboard first, one screen for the whole
 * sale (owner: «en una pantalla cerrar toda la operación»):
 *
 *   1. customer: /pedido/clientes (debounced), ↑ ↓ Enter, «Nuevo cliente»
 *      (/nueva-venta/cliente); his catalog priced with his list is loaded
 *      once (/nueva-venta/catalogo) and searched here, with no request per
 *      key;
 *   2. lines table: type part of the name → list → Enter → quantity →
 *      Enter → a new empty line; Supr / ⌫ on an empty line removes it;
 *   3. «¿Cómo sigue?» (Alt+1..4) and «Cargar la venta» (Ctrl+Enter):
 *      POST /nueva-venta/crear, the phone's endpoint (its JSON carries the
 *      order id);
 *   4. Mostrador: the depot delivery with its payment, POST
 *      /deposito/entrega/<id>?mostrador=1 (the depot form's own code);
 *   5. «Cobrar» (/nueva-venta/planilla/cobrar/<id>, distri.cobro) while the
 *      invoice owes, and the summary with the documents (new tab);
 *      «Nueva venta» (Alt+N).
 * 19.0.1.129.56 (owner: «cerrar la operación de mostrador en una pantalla»):
 * the steps of the sale on top (pasosDe), the customer's balance, «Cuenta
 * corriente» as a way to pay, «Corregir la venta» before the delivery
 * (/nueva-venta/planilla/corregir/<id>: «Anular venta» and the same lines
 * back to change and load again), and the remito to print or share.
 * The draft (customer, lines, exit, answers, and the order once loaded) is
 * kept in localStorage, so a failed step or a reload loses nothing.
 * 19.0.1.129.59 (administración only, body data-precios="1"; the server
 * checks it again): the «Precio» cell of each line can be typed over (the
 * customer's list price by default) and «Producto nuevo»
 * (/nueva-venta/producto) adds a product made on the spot at its price.
 *
 * The pure part (search, the lines' keyboard, shortcuts, amounts, where an
 * error goes) is window.DNPlanilla, tested in Node
 * (tests/data/planilla_v129_53.js); the rest draws it. */
(function () {
  'use strict';

  // ================================================================ pure core

  var MAX_ITEMS = 12;
  var MAX_CANTIDAD = 10000;

  function norm(text) {
    var s = String(text == null ? '' : text).toLowerCase();
    if (s.normalize) { s = s.normalize('NFD').replace(/[̀-ͯ]/g, ''); }
    return s.replace(/\s+/g, ' ').trim();
  }

  // the catalog rows matching every word typed; the ones whose name starts
  // with the text first, then a word of the name, then anywhere
  function buscarProductos(productos, q, limite) {
    var t = norm(q);
    if (!t) { return []; }
    var palabras = t.split(' ');
    var out = [];
    for (var i = 0; i < productos.length; i++) {
      var p = productos[i];
      var nombre = p._n || (p._n = norm(p.nombre));
      var todo = p._t || (p._t = nombre + ' ' + norm(p.seccion) + ' ' + norm(p.codigo));
      var ok = true;
      for (var j = 0; j < palabras.length; j++) {
        if (todo.indexOf(palabras[j]) === -1) { ok = false; break; }
      }
      if (!ok) { continue; }
      var rango = nombre.indexOf(t) === 0 ? 0 : ((' ' + nombre).indexOf(' ' + palabras[0]) !== -1 ? 1 : 2);
      out.push({p: p, r: rango, i: i});
    }
    out.sort(function (a, b) { return a.r - b.r || a.i - b.i; });
    return out.slice(0, limite || MAX_ITEMS).map(function (x) { return x.p; });
  }

  function cantidadValida(v) {
    var n = Math.floor(+String(v == null ? '' : v).replace(',', '.'));
    return isFinite(n) && n >= 1 ? Math.min(n, MAX_CANTIDAD) : 0;
  }

  // The lines table as state: rows {pid, texto, cantidad}, the focused cell
  // {i, campo: 'prod' | 'cant'} and the product list open under it.
  function Lineas(productos) {
    this.filas = [];
    this.extras = {};  // 19.0.1.129.59: products outside the catalog
    this.foco = {i: 0, campo: 'prod'};
    this.drop = {abierto: false, items: [], idx: 0};
    this.setCatalogo(productos || []);
    this.vaciar();
  }
  Lineas.prototype.setCatalogo = function (productos) {
    var self = this;
    this.productos = productos.slice();
    this.porId = {};
    productos.forEach(function (p) { self.porId[p.id] = p; });
    Object.keys(this.extras).forEach(function (id) {
      if (!self.porId[id]) { self.productos.push(self.extras[id]); self.porId[id] = self.extras[id]; }
    });
    // what is not in this customer's catalog leaves the table
    var fuera = [];
    this.filas = this.filas.filter(function (f) {
      if (f.pid && !self.porId[f.pid]) { fuera.push(f.texto); return false; }
      return true;
    });
    this._cola();
    return fuera;
  };
  Lineas.prototype.vaciar = function () {
    this.filas = [];
    this._cola();
    this.foco = {i: 0, campo: 'prod'};
    this.cerrar();
  };
  Lineas.prototype._vacia = function (f) { return !f.pid && !String(f.texto || '').trim(); };
  // always one empty line at the end
  Lineas.prototype._cola = function () {
    var ultima = this.filas[this.filas.length - 1];
    if (!ultima || !this._vacia(ultima)) { this.filas.push({pid: null, texto: '', cantidad: '', precio: ''}); }
  };
  Lineas.prototype.cerrar = function () { this.drop = {abierto: false, items: [], idx: 0}; };
  Lineas.prototype.enfocar = function (i, campo) {
    this.foco = {i: Math.max(0, Math.min(i, this.filas.length - 1)), campo: campo || 'prod'};
    this.cerrar();
  };
  Lineas.prototype.escribir = function (i, texto) {
    var f = this.filas[i];
    if (!f) { return; }
    f.texto = texto;
    if (f.pid && this.porId[f.pid] && texto !== this.porId[f.pid].nombre) { f.pid = null; }
    this.foco = {i: i, campo: 'prod'};
    var items = buscarProductos(this.productos, texto);
    this.drop = {abierto: items.length > 0 || !!norm(texto), items: items, idx: 0};
    this._cola();
  };
  // 19.0.1.129.59: a product made on the spot, kept whatever the customer
  Lineas.prototype.agregarExtra = function (p) {
    if (!p || !p.id) { return; }
    p.extra = true;
    this.extras[p.id] = p;
    if (!this.porId[p.id]) { this.productos.push(p); this.porId[p.id] = p; }
  };
  Lineas.prototype.setPrecio = function (i, v) {
    if (this.filas[i]) { this.filas[i].precio = String(v == null ? '' : v).replace(/[^0-9.,$ ]/g, '').slice(0, 14); }
  };
  // the line's unit price: the typed one, else the list's
  Lineas.prototype.precioDe = function (f) {
    var p = f && f.pid && this.porId[f.pid];
    return precioValido(f && f.precio) || (p && p.precio) || 0;
  };
  // the typed price differs from the list's (a product made on the spot
  // always carries its price)
  Lineas.prototype.precioCambiado = function (f) {
    var p = f && f.pid && this.porId[f.pid];
    var n = precioValido(f && f.precio);
    if (!p) { return 0; }
    if (p.extra) { return n || p.precio || 0; }
    return n && Math.abs(n - (p.precio || 0)) >= 0.005 ? n : 0;
  };
  // rows whose typed price cannot be read
  Lineas.prototype.preciosMalos = function () {
    return this.filas.filter(function (f) { return f.pid && String(f.precio || '').trim() && !precioValido(f.precio); }).length;
  };
  Lineas.prototype.setCantidad = function (i, v) {
    if (this.filas[i]) { this.filas[i].cantidad = String(v == null ? '' : v).replace(/[^0-9]/g, '').slice(0, 5); }
  };
  // picks product `pid` for row i; the same product twice is one line
  Lineas.prototype.elegir = function (i, pid) {
    var p = this.porId[pid];
    var f = this.filas[i];
    if (!p || !f) { return; }
    for (var j = 0; j < this.filas.length; j++) {
      if (j !== i && this.filas[j].pid === p.id) {
        this.filas.splice(i, 1);
        var k = j > i ? j - 1 : j;
        this._cola();
        this.enfocar(k, 'cant');
        return;
      }
    }
    f.pid = p.id;
    f.texto = p.nombre;
    if (!cantidadValida(f.cantidad)) { f.cantidad = '1'; }
    this._cola();
    this.enfocar(i, 'cant');
  };
  Lineas.prototype.borrar = function (i) {
    if (this.filas.length <= 1) { return; }
    this.filas.splice(i, 1);
    this._cola();
    if (i > 0) { this.enfocar(i - 1, this.filas[i - 1].pid ? 'cant' : 'prod'); } else { this.enfocar(0, 'prod'); }
  };
  Lineas.prototype._ultimaVacia = function () {
    this._cola();
    this.enfocar(this.filas.length - 1, 'prod');
  };
  // a key on the focused cell: true when the table used it (the page then
  // prevents the browser's default); 'salida' = go on to «¿Cómo sigue?»
  Lineas.prototype.tecla = function (key) {
    var i = this.foco.i;
    var f = this.filas[i];
    if (!f) { return false; }
    var d = this.drop;
    if (this.foco.campo === 'prod') {
      if (d.abierto && d.items.length && (key === 'ArrowDown' || key === 'ArrowUp')) {
        var n = d.items.length;
        d.idx = (d.idx + (key === 'ArrowDown' ? 1 : n - 1)) % n;
        return true;
      }
      if (key === 'Enter' || key === 'Tab') {
        if (d.abierto && d.items.length) { this.elegir(i, d.items[d.idx].id); return true; }
        if (key === 'Tab') { return false; }
        if (this._vacia(f)) { return this.renglones().length ? 'salida' : true; }
        if (f.pid) { this.enfocar(i, 'cant'); return true; }
        return true;  // text that matches nothing: stay
      }
      if (key === 'Escape') {
        if (d.abierto) {
          this.cerrar();
          if (f.pid === null && this.porId[f._antes]) { f.pid = f._antes; f.texto = this.porId[f._antes].nombre; }
          return true;
        }
        return false;
      }
      if ((key === 'Backspace' || key === 'Delete') && this._vacia(f) && this.filas.length > 1) {
        this.borrar(i);
        return true;
      }
      if (key === 'ArrowDown' && i < this.filas.length - 1) { this.enfocar(i + 1, 'prod'); return true; }
      if (key === 'ArrowUp' && i > 0) { this.enfocar(i - 1, 'prod'); return true; }
      return false;
    }
    // quantity
    if (key === 'Enter') {
      if (!cantidadValida(f.cantidad)) { f.cantidad = '1'; }
      this._ultimaVacia();
      return true;
    }
    if (key === 'ArrowDown') {
      if (i < this.filas.length - 1) { this.enfocar(i + 1, this.filas[i + 1].pid ? 'cant' : 'prod'); }
      return true;
    }
    if (key === 'ArrowUp') {
      if (i > 0) { this.enfocar(i - 1, 'cant'); }
      return true;
    }
    if (key === 'Escape') { this.enfocar(i, 'prod'); return true; }
    return false;
  };
  // what /nueva-venta/crear posts: [{id, cantidad}] (19.0.1.129.59: and
  // `precio` on a line whose price was changed)
  Lineas.prototype.renglones = function () {
    var self = this;
    return this.filas.filter(function (f) { return f.pid && cantidadValida(f.cantidad); })
      .map(function (f) {
        var r = {id: f.pid, cantidad: cantidadValida(f.cantidad)};
        var precio = self.precioCambiado(f);
        if (precio) { r.precio = precio; }
        return r;
      });
  };
  // rows with text that is not a product of the list
  Lineas.prototype.sueltas = function () {
    var self = this;
    return this.filas.filter(function (f) { return !f.pid && !self._vacia(f); }).length;
  };
  Lineas.prototype.cargar = function (renglones) {
    var self = this;
    this.filas = [];
    (renglones || []).forEach(function (r) {
      if (r.fila && !self.porId[r.id]) { self.agregarExtra(r.fila); }
      var p = self.porId[r.id];
      if (p && cantidadValida(r.cantidad)) {
        self.filas.push({pid: p.id, texto: p.nombre, cantidad: String(cantidadValida(r.cantidad)),
          precio: precioValido(r.precio) ? montoCampo(precioValido(r.precio)) : ''});
      }
    });
    this._cola();
    this.enfocar(this.filas.length - 1, 'prod');
  };
  Lineas.prototype.totales = function () {
    var total = 0, unidades = 0, productos = 0, presupuestar = 0;
    var self = this;
    this.filas.forEach(function (f) {
      var p = f.pid && self.porId[f.pid];
      var n = cantidadValida(f.cantidad);
      if (!p || !n) { return; }
      productos += 1;
      unidades += n;
      var precio = self.precioDe(f);
      if (precio > 0) { total += precio * n; } else { presupuestar += 1; }
    });
    return {total: total, unidades: unidades, productos: productos, presupuestar: presupuestar};
  };

  // the page's shortcuts (by physical key: Alt+letter types other
  // characters on some layouts)
  function atajo(ev) {
    var code = ev.code || '';
    var key = ev.key || '';
    if ((ev.ctrlKey || ev.metaKey) && !ev.altKey && key === 'Enter') { return 'cargar'; }
    if (!ev.altKey || ev.ctrlKey || ev.metaKey) { return null; }
    var digit = /^Digit([1-4])$/.exec(code) || /^Numpad([1-4])$/.exec(code);
    if (digit) { return 'salida:' + digit[1]; }
    if (code === 'KeyC') { return 'cliente'; }
    if (code === 'KeyP') { return 'productos'; }
    if (code === 'KeyN') { return 'nueva'; }
    if (code === 'KeyG') { return 'corregir'; }
    if (code === 'KeyR') { return 'remito'; }
    return null;
  }

  // «8.000», «8000,50», «$ 8000» → 8000 / 8000.5 (the server parses again)
  function parseMonto(text) {
    var raw = String(text == null ? '' : text).replace(/\$/g, '').replace(/\s/g, '');
    if (!raw) { return 0; }
    if (raw.indexOf(',') !== -1) { raw = raw.replace(/\./g, '').replace(',', '.'); }
    else if (/^\d{1,3}(\.\d{3})+$/.test(raw)) { raw = raw.replace(/\./g, ''); }
    var n = parseFloat(raw);
    return isFinite(n) ? n : 0;
  }
  // an amount to type in a field: «8000» or «8000,50», no thousands dots
  function montoCampo(n) {
    var c = Math.round((n || 0) * 100);
    if (c <= 0) { return ''; }
    return c % 100 ? Math.floor(c / 100) + ',' + String(c % 100).padStart(2, '0') : String(c / 100);
  }
  // 19.0.1.129.59: a typed unit price, above 0 (models/nueva_venta.py
  // precio_valido), else 0
  function precioValido(text) {
    var n = parseMonto(text);
    return n > 0 && n <= 1e8 ? Math.round(n * 100) / 100 : 0;
  }
  function plata(n) {
    var c = Math.round((n || 0) * 100) / 100;
    var s = Math.abs(c).toLocaleString('es-AR', {minimumFractionDigits: c % 1 ? 2 : 0, maximumFractionDigits: 2});
    return (c < 0 ? '- ' : '') + '$ ' + s;
  }

  // which field an error of the server belongs next to
  function ubicarError(texto) {
    var t = norm(texto);
    if (/whatsapp/.test(t)) { return 'wa'; }
    if (/motivo de la entrega|fecha|dia en que/.test(t)) { return 'motivo'; }
    if (/devolvi|devuelta|motivo de lo que/.test(t)) { return 'devolucion'; }
    if (/comprobante|archivo|transferencia|subir/.test(t)) { return 'comprobante'; }
    if (/cliente/.test(t) && !/producto/.test(t)) { return 'cliente'; }
    if (/producto|catalogo|carrito|cantidad/.test(t)) { return 'lineas'; }
    return '';
  }

  // 19.0.1.129.56: where the sale stands, for the steps on top. Each step is
  // 'hecho', 'ahora', 'no' (does not apply here / not yet) or ''.
  function pasosDe(v) {
    if (!v) { return {carga: 'ahora', cargada: '', entregada: '', cobrada: ''}; }
    if (v.estado === 'cancel') { return {carga: 'hecho', cargada: 'no', entregada: 'no', cobrada: 'no'}; }
    var p = {carga: 'hecho', cargada: 'hecho', entregada: '', cobrada: ''};
    if (v.entrega) { p.entregada = 'ahora'; return p; }
    // «A armar» and «Presupuesto» go on in Armado, not on this screen
    if (!v.entregado) { p.entregada = 'no'; p.cobrada = 'no'; return p; }
    p.entregada = 'hecho';
    if (v.cobrar) { p.cobrada = 'ahora'; } else if (v.facturada && !(v.residual > 0.005)) { p.cobrada = 'hecho'; } else { p.cobrada = 'no'; }
    return p;
  }
  // the names of the steps that change with the sale
  function textoPasos(v) {
    var t = {cargada: 'Cargada', cobrada: 'Cobrada'};
    if (!v) { return t; }
    if (v.estado === 'cancel') { t.cargada = 'Anulada'; return t; }
    t.cargada = v.facturada ? 'Facturada' : (v.estado === 'sale' ? 'Confirmada · va a Armado' : 'Presupuesto guardado');
    if (v.entregado && !v.cobrar && v.residual > 0.005) {
      t.cobrada = v.en_camino > 0.005 && v.residual - v.en_camino <= 0.005 ? 'Transferencia a verificar' : 'En cuenta corriente';
    }
    return t;
  }
  // the customer's balance as a chip: {texto, clase} or null (not shown)
  function chipSaldo(c) {
    if (!c || c.saldo == null) { return null; }
    if (c.saldo > 0.005) { return {texto: 'Debe ' + c.saldo_texto, clase: 'dn-chip--warn'}; }
    if (c.saldo < -0.005) { return {texto: 'A favor ' + c.saldo_texto, clase: 'dn-chip--ok'}; }
    return {texto: 'Al día', clase: 'dn-chip--ok'};
  }

  // the layout this device opens: its own choice, else by the screen
  function elegirVista(pref, pc) {
    if (pref === 'planilla' || pref === 'celular') { return pref; }
    return pc ? 'planilla' : 'celular';
  }

  var core = {norm: norm, buscarProductos: buscarProductos, Lineas: Lineas, atajo: atajo,
    parseMonto: parseMonto, montoCampo: montoCampo, plata: plata, ubicarError: ubicarError,
    elegirVista: elegirVista, cantidadValida: cantidadValida, VISTA: 'dn:nv:vista',
    pasosDe: pasosDe, textoPasos: textoPasos, chipSaldo: chipSaldo, precioValido: precioValido};
  window.DNPlanilla = core;
  if (typeof document === 'undefined' || !document.getElementById || !document.getElementById('pl-form')) {
    return;
  }

  // ================================================================ the page

  var $ = function (id) { return document.getElementById(id); };
  var h = function (v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) {
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c];
    });
  };
  var body = document.body;
  var UID = body.getAttribute('data-uid') || '0';
  var KEY = 'dn:nv:planilla:' + UID;
  var DIAS_BORRADOR = 7;
  var MAX_TRANSF = +body.getAttribute('data-max-transf') || 5;
  var RECEIPT_MAX_BYTES = 10 * 1024 * 1024;
  var RECEIPT_TYPES = /^(image\/(jpeg|png|webp|heic|heif)|application\/pdf)$/i;
  var RECEIPT_NAMES = /\.(jpe?g|png|webp|heic|heif|pdf)$/i;
  var form = $('pl-form');
  var tbody = $('pl-filas');
  var lineas = new Lineas([]);
  var cliente = null;     // /nueva-venta/catalogo's «cliente»
  var anteriores = [];
  var venta = null;       // the summary of the loaded sale
  var pendiente = null;   // the id of a sale loaded whose summary did not come yet
  var envio = '';
  var restaurando = false;
  var archivos = [];      // receipts of the delivery's transfer
  var transf = [];        // «Cobrar»: [{file, importe, recibo, nombre}]
  var flagsCobro = {};
  var PRECIOS = body.getAttribute('data-precios') === '1';  // 19.0.1.129.59

  function guardar(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } }
  function leer(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function quitar(k) { try { localStorage.removeItem(k); } catch (e) { /* private mode */ } }

  // ?vista=planilla: «Ver versión planilla» from the phone layout
  if (/[?&]vista=planilla/.test(location.search)) {
    guardar(core.VISTA, 'planilla');
    try { history.replaceState(history.state, '', location.pathname); } catch (e) { /* old */ }
  }
  $('pl-celular').addEventListener('click', function () { guardar(core.VISTA, 'celular'); });

  function nuevoEnvio() {
    var bytes = new Uint8Array(16);
    if (window.crypto && crypto.getRandomValues) { crypto.getRandomValues(bytes); } else {
      for (var i = 0; i < 16; i++) { bytes[i] = Math.floor(Math.random() * 256); }
    }
    return Array.prototype.map.call(bytes, function (b) { return ('0' + b.toString(16)).slice(-2); }).join('');
  }

  // ------------------------------------------------------------ fetch

  function csrf() { var m = document.querySelector('meta[name="csrf-token"]'); return m ? m.content : ''; }
  // {status, data}; data.error on a refusal; a network failure rejects
  function pedir(url, fd, timeout) {
    if (fd && !fd.has('csrf_token')) { fd.append('csrf_token', csrf()); }
    var ctrl = window.AbortController ? new AbortController() : null;
    var timer = ctrl && setTimeout(function () { ctrl.abort(); }, timeout || 30000);
    return fetch(url, {method: fd ? 'POST' : 'GET', body: fd || undefined, credentials: 'same-origin',
      signal: ctrl && ctrl.signal, headers: {'X-Distri-Fetch': '1', 'Accept': 'application/json'}})
      .then(function (r) {
        clearTimeout(timer);
        if (r.redirected && /\/web\/login/.test(r.url)) {
          location.href = '/web/login?redirect=' + encodeURIComponent('/nueva-venta/planilla');
          return {status: 401, data: {ok: false, error: 'Se cerró la sesión. Entrá de nuevo.'}};
        }
        var type = r.headers.get('Content-Type') || '';
        if (type.indexOf('json') !== -1) {
          return r.json().then(function (j) {
            if (Array.isArray(j)) { return {status: r.status, data: j}; }
            if (!r.ok && j.ok !== false) { j = {ok: false, error: j.error || 'No se pudo guardar.'}; }
            return {status: r.status, data: j};
          });
        }
        return {status: r.status, data: {ok: false, error: r.status === 400
          ? 'La página venció: recargala (lo cargado no se pierde).'
          : 'Hubo un error (' + r.status + '). Probá de nuevo: lo cargado no se pierde.'}};
      }, function () {
        clearTimeout(timer);
        return {status: 0, data: {ok: false, error: 'Sin conexión: no se guardó. Probá de nuevo, lo cargado no se pierde.'}};
      });
  }

  // ------------------------------------------------------------ errors next to their field

  var ERR = {cliente: 'pl-err-cliente', lineas: 'pl-err-lineas'};
  function errEl(key, fallback) {
    if (ERR[key]) { return $(ERR[key]); }
    var el = key && document.querySelector('[data-pl-err="' + key + '"]');
    return (el && el.offsetParent !== null) ? el : $(fallback);
  }
  function limpiarErrores() {
    Array.prototype.forEach.call(document.querySelectorAll('.pl-err'), function (el) {
      el.hidden = true; el.textContent = '';
    });
  }
  function mostrarError(texto, fallback) {
    var el = errEl(ubicarError(texto), fallback);
    el.textContent = texto;
    el.hidden = false;
    if (el.scrollIntoView) { el.scrollIntoView({block: 'nearest'}); }
    return el;
  }

  // ------------------------------------------------------------ draft

  var CAMPOS = ['entrega', 'telefono', 'domicilio', 'comentario', 'nota_deposito', 'contacto', 'fecha', 'motivo',
    'wa_area', 'wa_numero', 'enc_nombre', 'enc_dni', 'enc_localidad'];
  function guardarBorrador() {
    if (restaurando || !cliente) { return; }
    var campos = {};
    CAMPOS.forEach(function (n) { var el = form.elements[n]; if (el) { campos[n] = el.value; } });
    guardar(KEY, JSON.stringify({t: Date.now(), cliente: {id: cliente.id, nombre: cliente.nombre},
      renglones: lineas.renglones(), salida: salidaActual(), campos: campos, envio: envio,
      encomienda: $('pl-encomienda').checked, venta_id: venta ? venta.id : pendiente,
      extras: PRECIOS ? lineas.extras : {}}));
  }
  function leerBorrador() {
    try {
      var b = JSON.parse(leer(KEY) || 'null');
      if (!b || !b.cliente || (Date.now() - (b.t || 0)) > DIAS_BORRADOR * 864e5) { return null; }
      return b;
    } catch (e) { return null; }
  }

  // ------------------------------------------------------------ customer

  var cli = {items: [], idx: 0, n: 0, espera: null};
  function pintarClientes() {
    var box = $('pl-clientes');
    var q = $('pl-q-cliente').value.trim();
    if (q.length < 2) { box.hidden = true; $('pl-q-cliente').setAttribute('aria-expanded', 'false'); return; }
    var html = cli.items.map(function (f, i) {
      return '<li role="option" id="pl-cli-' + i + '" data-i="' + i + '" aria-selected="' + (i === cli.idx) + '"' +
        (i === cli.idx ? ' class="on"' : '') + '><b>' + h(f.nombre) + '</b><small>' +
        h([f.domicilio, f.ciudad, f.whatsapp, f.mio ? 'tu cliente' : ''].filter(Boolean).join(' · ')) +
        '</small></li>';
    }).join('');
    var n = cli.items.length;
    html += '<li role="option" id="pl-cli-' + n + '" data-i="' + n + '" data-nuevo="1" aria-selected="' +
      (cli.idx === n) + '" class="pl-drop__nuevo' + (cli.idx === n ? ' on' : '') + '">+ Nuevo cliente «' +
      h(q) + '»</li>';
    box.innerHTML = html;
    box.hidden = false;
    $('pl-q-cliente').setAttribute('aria-expanded', 'true');
    $('pl-q-cliente').setAttribute('aria-activedescendant', 'pl-cli-' + cli.idx);
  }
  function buscarClientes() {
    var q = $('pl-q-cliente').value.trim();
    var n = ++cli.n;
    if (q.length < 2) { cli.items = []; pintarClientes(); return; }
    pedir('/pedido/clientes?q=' + encodeURIComponent(q)).then(function (r) {
      if (n !== cli.n) { return; }
      cli.items = Array.isArray(r.data) ? r.data : [];
      cli.idx = 0;
      pintarClientes();
    });
  }
  $('pl-q-cliente').addEventListener('input', function () {
    clearTimeout(cli.espera);
    cli.espera = setTimeout(buscarClientes, 180);
  });
  $('pl-q-cliente').addEventListener('keydown', function (ev) {
    var box = $('pl-clientes');
    var total = cli.items.length + 1;
    if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
      if (box.hidden) { return; }
      ev.preventDefault();
      cli.idx = (cli.idx + (ev.key === 'ArrowDown' ? 1 : total - 1)) % total;
      pintarClientes();
    } else if (ev.key === 'Enter') {
      ev.preventDefault();
      if (box.hidden) { buscarClientes(); return; }
      elegirDeLista(cli.idx);
    } else if (ev.key === 'Escape') {
      box.hidden = true;
    }
  });
  $('pl-clientes').addEventListener('mousedown', function (ev) {
    var li = ev.target.closest('li[data-i]');
    if (li) { ev.preventDefault(); elegirDeLista(+li.dataset.i); }
  });
  function elegirDeLista(i) {
    $('pl-clientes').hidden = true;
    if (i >= cli.items.length) { abrirNuevo(); return; }
    elegirCliente(cli.items[i]);
  }

  function elegirCliente(f, borrador) {
    limpiarErrores();
    $('pl-q-cliente').disabled = true;
    return pedir('/nueva-venta/catalogo?cliente_id=' + encodeURIComponent(f.id)).then(function (r) {
      $('pl-q-cliente').disabled = false;
      if (!r.data.ok) {
        mostrarError(r.data.error || 'No se pudo abrir el cliente.', 'pl-err-cliente');
        return false;
      }
      if (cliente && cliente.id !== r.data.cliente.id) {
        ['wa_area', 'wa_numero', 'contacto', 'telefono', 'domicilio'].forEach(function (n) {
          if (form.elements[n]) { form.elements[n].value = ''; }
        });
      }
      cliente = r.data.cliente;
      $('pl-cliente-id').value = cliente.id;
      var fuera = lineas.setCatalogo(r.data.productos || []);
      if (fuera.length) {
        mostrarError('Salieron de la venta (no están en el catálogo de este cliente): ' + fuera.join(', ') + '.',
          'pl-err-lineas');
      }
      pintarCliente();
      pintarAnteriores(r.data.anteriores || []);
      if (borrador) { restaurar(borrador); }
      pintarFilas();
      pintarSalida();
      guardarBorrador();
      if (!borrador || !borrador.venta_id) { enfocarProducto(lineas.filas.length - 1); }
      return true;
    });
  }

  function pintarCliente() {
    var c = cliente;
    $('pl-buscar-cliente').hidden = true;
    $('pl-elegido').hidden = false;
    $('pl-nuevo').hidden = true;
    $('pl-cliente-nombre').textContent = c.nombre;
    $('pl-cliente-meta').textContent = [c.domicilio, c.whatsapp].filter(Boolean).join(' · ');
    var chip = chipSaldo(c);
    $('pl-saldo').hidden = !chip;
    if (chip) {
      $('pl-saldo').textContent = chip.texto;
      $('pl-saldo').className = 'dn-chip ' + chip.clase;
    }
    $('pl-lista').hidden = !c.lista;
    $('pl-lista').textContent = c.lista ? 'Lista: ' + c.lista : '';
    $('pl-precios-de').textContent = c.lista ? '· precios de ' + c.lista : '';
    $('pl-cuenta').hidden = !c.cuenta;
    if (c.cuenta) { $('pl-cuenta').href = c.cuenta; }
    $('pl-ficha').hidden = !c.ficha;
    if (c.ficha) { $('pl-ficha').href = c.ficha; }
    $('pl-sin-cliente').hidden = true;
    $('pl-tabla').hidden = false;
    $('pl-entrega-resumen').textContent = [c.domicilio, c.whatsapp].filter(Boolean).join(' · ') ||
      '(su ficha no tiene domicilio ni teléfono)';
    if (!$('pl-telefono').value) { $('pl-telefono').value = c.whatsapp || ''; }
    if (!$('pl-domicilio').value) { $('pl-domicilio').value = c.domicilio || ''; }
    if (c.sin_whatsapp && !$('pl-wa-area').value && !$('pl-wa-numero').value) {
      $('pl-wa-area').value = c.wa_area || '';
      $('pl-wa-numero').value = c.wa_numero || '';
    }
  }

  function pintarAnteriores(lista) {
    anteriores = lista;
    var box = $('pl-anteriores');
    box.hidden = !lista.length || !!venta;
    box.innerHTML = lista.length ? '<span class="nv-meta">Repetir:</span>' + lista.map(function (a, i) {
      return '<button type="button" class="dn-btn dn-btn--ghost dn-btn--sm dn-btn--auto" data-rep="' + i + '">' +
        h(a.nombre) + ' <small class="nv-meta">' + h(a.fecha + ' · ' + a.total) + '</small></button>';
    }).join('') : '';
  }
  $('pl-anteriores').addEventListener('click', function (ev) {
    var b = ev.target.closest('button[data-rep]');
    if (!b || venta) { return; }
    lineas.cargar(anteriores[+b.dataset.rep].renglones);
    pintarFilas();
    guardarBorrador();
  });

  function cambiarCliente() {
    if (venta) { return; }
    $('pl-elegido').hidden = true;
    $('pl-anteriores').hidden = true;
    $('pl-buscar-cliente').hidden = false;
    $('pl-q-cliente').value = '';
    $('pl-q-cliente').focus();
  }
  $('pl-cambiar').addEventListener('click', cambiarCliente);

  // «Nuevo cliente»
  var nuevoOk = false;
  function abrirNuevo() {
    nuevoOk = false;
    $('pl-nuevo').hidden = false;
    $('pl-parecidos').hidden = true;
    $('pl-nuevo-crear').textContent = 'Crear y elegir';
    ['pl-n-area', 'pl-n-numero', 'pl-n-calle', 'pl-n-ciudad'].forEach(function (id) { $(id).value = ''; });
    var q = $('pl-q-cliente').value.trim();
    var digitos = q.replace(/\D/g, '');
    $('pl-n-nombre').value = digitos.length >= 6 ? '' : q;
    $('pl-n-nombre').focus();
  }
  $('pl-nuevo-cancelar').addEventListener('click', function () {
    $('pl-nuevo').hidden = true;
    $('pl-q-cliente').focus();
  });
  ['pl-n-nombre', 'pl-n-area', 'pl-n-numero'].forEach(function (id) {
    $(id).addEventListener('input', function () { nuevoOk = false; $('pl-nuevo-crear').textContent = 'Crear y elegir'; });
  });
  $('pl-nuevo').addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter' && ev.target.tagName === 'INPUT') { ev.preventDefault(); $('pl-nuevo-crear').click(); }
    if (ev.key === 'Escape') { $('pl-nuevo-cancelar').click(); }
  });
  $('pl-parecidos').addEventListener('click', function (ev) {
    var b = ev.target.closest('button[data-cli]');
    if (b) { elegirCliente({id: +b.dataset.cli}); }
  });
  $('pl-nuevo-crear').addEventListener('click', function () {
    var btn = $('pl-nuevo-crear');
    var fd = new FormData();
    fd.append('nombre', $('pl-n-nombre').value);
    fd.append('wa_area', $('pl-n-area').value);
    fd.append('wa_numero', $('pl-n-numero').value);
    fd.append('calle', $('pl-n-calle').value);
    fd.append('ciudad', $('pl-n-ciudad').value);
    fd.append('nuevo_ok', nuevoOk ? '1' : '');
    btn.disabled = true;
    $('pl-err-nuevo').hidden = true;
    pedir('/nueva-venta/cliente', fd).then(function (r) {
      btn.disabled = false;
      if (!r.data.ok) {
        $('pl-err-nuevo').textContent = r.data.error;
        $('pl-err-nuevo').hidden = false;
        return;
      }
      if (r.data.parecidos) {
        $('pl-parecidos').innerHTML = '<p class="nv-meta"><b>Puede que ya esté cargado.</b> Si es uno de estos, ' +
          'elegilo:</p>' + r.data.parecidos.map(function (p) {
            return '<button type="button" class="nv-item" data-cli="' + (+p.id) + '"><b>' + h(p.name) +
              '</b><small>' + h([p.why, p.detail].filter(Boolean).join(' · ')) + '</small></button>';
          }).join('');
        $('pl-parecidos').hidden = false;
        nuevoOk = true;
        btn.textContent = 'No es ninguno: crear igual';
        return;
      }
      elegirCliente(r.data.cliente);
    });
  });

  // ------------------------------------------------------------ lines table

  function celdaPrecio(p, f, i, bloqueada) {
    if (!p) { return ''; }
    var precio = lineas.precioDe(f);
    if (PRECIOS && !bloqueada) {
      // 19.0.1.129.59: typed over (administración); the list's by default
      var cambiado = !!lineas.precioCambiado(f) && !p.extra;
      return '<input class="pl-cell pl-pre dn-num' + (cambiado ? ' pl-pre--cambiado' : '') + '" data-i="' + i +
        '" value="' + h(f.precio || montoCampo(p.precio)) + '" inputmode="decimal" autocomplete="off"' +
        ' title="' + h(p.extra ? 'Producto nuevo' : 'Precio de lista: ' + (p.precio > 0 ? plata(p.precio) : 'a presupuestar')) +
        '" aria-label="Precio unitario del renglón ' + (i + 1) + '"/>';
    }
    return precio > 0 ? plata(precio) : '<span class="pl-presup">a presupuestar</span>';
  }
  function celdaSub(f) {
    var p = f.pid && lineas.porId[f.pid];
    var n = cantidadValida(f.cantidad);
    if (!p || !n) { return ''; }
    var precio = lineas.precioDe(f);
    return precio > 0 ? plata(precio * n) : '<span class="pl-presup">a presupuestar</span>';
  }
  function pintarFilas() {
    var bloqueada = !!venta;
    tbody.innerHTML = lineas.filas.map(function (f, i) {
      var p = f.pid && lineas.porId[f.pid];
      var n = cantidadValida(f.cantidad);
      if (bloqueada && !p) { return ''; }
      var unidad = p ? h(p.unidad + (p.contiene ? ' x ' + p.contiene : '')) : '';
      var sub = celdaSub(f);
      return '<tr data-i="' + i + '"' + (p ? '' : ' class="pl-fila--vacia"') + '><td class="pl-c-n dn-num">' +
        (i + 1) + '</td><td class="pl-c-prod"><input class="pl-cell pl-prod" data-i="' + i + '" value="' +
        h(f.texto) + '" title="' + h(f.texto) + '" autocomplete="off" spellcheck="false" aria-label="Producto del renglón ' + (i + 1) + '"' +
        ' role="combobox" aria-autocomplete="list" aria-controls="pl-productos" aria-expanded="false"' +
        (bloqueada ? ' disabled="disabled"' : '') + (p ? '' : ' placeholder="Escribí parte del nombre…"') +
        '/></td><td class="pl-c-cant"><input class="pl-cell pl-cant dn-num" data-i="' + i + '" value="' +
        h(f.cantidad) + '" inputmode="numeric" autocomplete="off" aria-label="Cantidad del renglón ' + (i + 1) + '"' +
        (bloqueada || !p ? ' tabindex="-1"' : '') + (bloqueada ? ' disabled="disabled"' : '') + '/></td>' +
        '<td class="pl-c-uni">' + unidad + '</td><td class="pl-c-pre dn-num">' + celdaPrecio(p, f, i, bloqueada) + '</td>' +
        '<td class="pl-c-sub dn-num" data-sub="' + i + '">' + sub + '</td><td class="pl-c-x">' +
        (bloqueada || !p ? '' : '<button type="button" class="pl-x" data-x="' + i + '" tabindex="-1" ' +
          'aria-label="Quitar ' + h(f.texto) + '">×</button>') + '</td></tr>';
    }).join('');
    if ($('pl-producto-abrir')) { $('pl-producto-abrir').hidden = bloqueada || !cliente; }
    pintarTotales();
    pintarDrop();
  }
  function pintarTotales() {
    var t = lineas.totales();
    $('pl-total').textContent = plata(t.total);
    $('pl-total-lado').textContent = plata(t.total);
    $('pl-conteo').textContent = t.productos ? t.productos + (t.productos === 1 ? ' producto, ' : ' productos, ') +
      t.unidades + (t.unidades === 1 ? ' unidad' : ' unidades') +
      (t.presupuestar ? ' · ' + t.presupuestar + ' a presupuestar' : '') : '';
    $('pl-renglones').value = JSON.stringify(lineas.renglones());
    lineas.filas.forEach(function (f, i) {
      var td = tbody.querySelector('[data-sub="' + i + '"]');
      if (td) { td.innerHTML = celdaSub(f); }
      var pre = PRECIOS && inputDe(i, 'pre');
      var p = f.pid && lineas.porId[f.pid];
      if (pre && p) { pre.classList.toggle('pl-pre--cambiado', !!lineas.precioCambiado(f) && !p.extra); }
    });
    pintarListo();
  }
  function inputDe(i, campo) { return tbody.querySelector('input.pl-' + campo + '[data-i="' + i + '"]'); }
  function pintarDrop() {
    var box = $('pl-productos');
    var d = lineas.drop;
    var input = inputDe(lineas.foco.i, 'prod');
    tbody.querySelectorAll('input.pl-prod').forEach(function (el) { el.setAttribute('aria-expanded', 'false'); });
    if (!d.abierto || !input || venta) { box.hidden = true; return; }
    box.innerHTML = d.items.length ? d.items.map(function (p, k) {
      return '<li role="option" id="pl-prod-' + k + '" data-id="' + p.id + '" aria-selected="' + (k === d.idx) + '"' +
        (k === d.idx ? ' class="on"' : '') + '><span class="pl-drop__n">' + h(p.nombre) + '<small>' +
        h([p.unidad + (p.contiene ? ' x ' + p.contiene : ''), p.seccion].filter(Boolean).join(' · ')) +
        '</small></span><span class="pl-drop__p dn-num">' + (p.precio > 0 ? plata(p.precio) : 'a presupuestar') +
        '</span></li>';
    }).join('') : '<li class="pl-drop__nada">Ningún producto coincide.</li>';
    var cont = box.parentNode.getBoundingClientRect();
    var r = input.getBoundingClientRect();
    box.style.top = (r.bottom - cont.top + 2) + 'px';
    box.style.left = (r.left - cont.left) + 'px';
    box.style.width = Math.max(r.width, 460) + 'px';
    box.hidden = false;
    input.setAttribute('aria-expanded', 'true');
    input.setAttribute('aria-activedescendant', d.items.length ? 'pl-prod-' + d.idx : '');
    var on = box.querySelector('li.on');
    if (on && on.scrollIntoView) { on.scrollIntoView({block: 'nearest'}); }
  }
  function aplicarFoco() {
    var el = inputDe(lineas.foco.i, lineas.foco.campo);
    if (el && document.activeElement !== el) {
      el.focus();
      if (el.select) { el.select(); }
    }
  }
  function enfocarProducto(i) {
    if (venta || !cliente) { return; }
    lineas.enfocar(i == null ? lineas.filas.length - 1 : i, 'prod');
    pintarFilas();
    aplicarFoco();
  }

  tbody.addEventListener('input', function (ev) {
    var el = ev.target;
    var i = +el.dataset.i;
    if (el.classList.contains('pl-pre')) {
      // 19.0.1.129.59: the typed price, without drawing the row again
      lineas.setPrecio(i, el.value);
      pintarTotales();
      guardarBorrador();
      return;
    }
    if (el.classList.contains('pl-prod')) {
      var f = lineas.filas[i];
      if (f && f.pid) { f._antes = f.pid; }
      var antes = lineas.filas.length;
      lineas.escribir(i, el.value);
      if (lineas.filas.length !== antes) {
        // a new trailing line: draw it without losing the caret
        var pos = el.selectionStart;
        pintarFilas();
        var again = inputDe(i, 'prod');
        if (again) { again.focus(); try { again.setSelectionRange(pos, pos); } catch (e) { /* type */ } }
      } else {
        pintarDrop();
      }
      pintarTotales();
    } else if (el.classList.contains('pl-cant')) {
      lineas.setCantidad(i, el.value);
      if (el.value !== lineas.filas[i].cantidad) { el.value = lineas.filas[i].cantidad; }
      pintarTotales();
    }
    guardarBorrador();
  });
  tbody.addEventListener('focusin', function (ev) {
    var el = ev.target;
    if (!el.dataset || el.dataset.i == null || el.classList.contains('pl-pre')) { return; }
    var campo = el.classList.contains('pl-cant') ? 'cant' : 'prod';
    if (lineas.foco.i !== +el.dataset.i || lineas.foco.campo !== campo) {
      lineas.foco = {i: +el.dataset.i, campo: campo};
      lineas.cerrar();
      pintarDrop();
    }
  });
  tbody.addEventListener('focusout', function () {
    setTimeout(function () {
      if (!tbody.contains(document.activeElement)) { lineas.cerrar(); pintarDrop(); }
    }, 120);
  });
  tbody.addEventListener('keydown', function (ev) {
    if (ev.altKey || ev.ctrlKey || ev.metaKey) { return; }
    var el = ev.target;
    if (!el.dataset || el.dataset.i == null || el.classList.contains('pl-pre')) { return; }
    if (ev.key === 'Tab' && ev.shiftKey) { return; }
    lineas.foco = {i: +el.dataset.i, campo: el.classList.contains('pl-cant') ? 'cant' : 'prod'};
    var antes = JSON.stringify([lineas.filas.length, lineas.foco, lineas.filas.map(function (f) { return f.pid; })]);
    var r = lineas.tecla(ev.key);
    if (!r) { return; }
    ev.preventDefault();
    if (r === 'salida') { irASalida(); return; }
    var despues = JSON.stringify([lineas.filas.length, lineas.foco, lineas.filas.map(function (f) { return f.pid; })]);
    if (antes !== despues) { pintarFilas(); aplicarFoco(); } else { pintarDrop(); }
    guardarBorrador();
  });
  tbody.addEventListener('click', function (ev) {
    var x = ev.target.closest('button[data-x]');
    if (!x) { return; }
    lineas.borrar(+x.dataset.x);
    pintarFilas();
    aplicarFoco();
    guardarBorrador();
  });
  $('pl-productos').addEventListener('mousedown', function (ev) {
    var li = ev.target.closest('li[data-id]');
    if (!li) { return; }
    ev.preventDefault();
    lineas.elegir(lineas.foco.i, +li.dataset.id);
    pintarFilas();
    aplicarFoco();
    guardarBorrador();
  });

  // ------------------------------------------------------------ «Producto nuevo» (129.59)

  if (PRECIOS && $('pl-producto')) {
    var cerrarProducto = function () {
      $('pl-producto').hidden = true;
      enfocarProducto();
    };
    $('pl-producto-abrir').addEventListener('click', function () {
      var f = lineas.filas[lineas.foco.i];
      $('pl-p-nombre').value = f && !f.pid ? String(f.texto || '').trim() : '';
      $('pl-p-precio').value = '';
      $('pl-p-cantidad').value = '1';
      $('pl-err-producto').hidden = true;
      $('pl-producto').hidden = false;
      $('pl-p-nombre').focus();
    });
    $('pl-producto-cancelar').addEventListener('click', cerrarProducto);
    $('pl-producto').addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' && ev.target.tagName === 'INPUT') { ev.preventDefault(); ev.stopPropagation(); $('pl-producto-crear').click(); }
      if (ev.key === 'Escape') { cerrarProducto(); }
    });
    $('pl-producto-crear').addEventListener('click', function () {
      var err = $('pl-err-producto');
      var nombre = $('pl-p-nombre').value.trim();
      var precio = precioValido($('pl-p-precio').value);
      var cantidad = cantidadValida($('pl-p-cantidad').value) || 1;
      err.hidden = true;
      if (nombre.length < 2) { err.textContent = 'Escribí el nombre del producto.'; err.hidden = false; return; }
      if (!precio) { err.textContent = 'Poné el precio: un número mayor que cero.'; err.hidden = false; return; }
      var fd = new FormData();
      fd.append('nombre', nombre);
      fd.append('precio', String(precio));
      var btn = $('pl-producto-crear');
      btn.disabled = true;
      pedir('/nueva-venta/producto', fd).then(function (r) {
        btn.disabled = false;
        if (!r.data.ok) { err.textContent = r.data.error || 'No se pudo crear el producto.'; err.hidden = false; return; }
        var p = r.data.producto;
        lineas.agregarExtra(p);
        // in the row being typed when it was empty or unmatched, else a new one
        var i = lineas.foco.i;
        var f = lineas.filas[i];
        if (!f || f.pid) { i = lineas.filas.length - 1; }
        lineas.elegir(i, p.id);
        var k = lineas.foco.i;
        lineas.setCantidad(k, String(cantidad));
        lineas.setPrecio(k, montoCampo(p.precio));
        $('pl-producto').hidden = true;
        pintarFilas();
        aplicarFoco();
        guardarBorrador();
      });
    });
  }

  // ------------------------------------------------------------ the steps on top

  function pintarPasos() {
    var estado = pasosDe(venta);
    var texto = textoPasos(venta);
    Array.prototype.forEach.call(document.querySelectorAll('#pl-pasos [data-paso]'), function (li) {
      var k = li.getAttribute('data-paso');
      li.className = estado[k] ? 'pl-paso--' + estado[k] : '';
      if (estado[k] === 'ahora') { li.setAttribute('aria-current', 'step'); } else { li.removeAttribute('aria-current'); }
      var t = li.querySelector('[data-paso-t]');
      if (t && texto[k]) { t.textContent = texto[k]; }
    });
  }

  // ------------------------------------------------------------ «¿Cómo sigue?»

  var ops = Array.prototype.slice.call(document.querySelectorAll('#pl-salidas [data-salida]'));
  function salidaActual() { return $('pl-salida').value; }
  function elegirSalida(valor, enfocar) {
    if (venta) { return; }
    $('pl-salida').value = valor;
    pintarSalida();
    guardarBorrador();
    var op = ops.filter(function (b) { return b.dataset.salida === valor; })[0];
    if (enfocar && op) { op.focus(); }
  }
  function pintarSalida() {
    var s = salidaActual();
    ops.forEach(function (b) {
      var on = b.dataset.salida === s;
      b.setAttribute('aria-checked', on ? 'true' : 'false');
      b.tabIndex = on || (!s && b === ops[0]) ? 0 : -1;
    });
    var op = ops.filter(function (b) { return b.dataset.salida === s; })[0];
    $('pl-salida-ayuda').textContent = op ? op.title : 'Elegí cómo sigue: Alt+1 a Alt+' + ops.length + '.';
    Array.prototype.forEach.call(document.querySelectorAll('[data-pl-para]'), function (el) {
      el.hidden = el.dataset.plPara.split(' ').indexOf(s) === -1;
    });
    $('pl-wa').hidden = !(cliente && cliente.sin_whatsapp && s && s !== 'presupuesto');
    var on = $('pl-encomienda').checked;
    $('pl-enc').hidden = !on;
    pintarListo();
  }
  function pintarListo() {
    var s = salidaActual();
    var hay = lineas.renglones().length > 0;
    $('pl-cargar').disabled = !(s && hay && cliente) || !!venta;
    var txt = {armar: 'Confirmar y mandar a Armado', mostrador: 'Vender en el mostrador',
      entregado: 'Cargar como entregada', presupuesto: 'Guardar el presupuesto'}[s] || 'Cargar la venta';
    $('pl-cargar').firstChild.textContent = txt + ' ';
  }
  ops.forEach(function (b, k) {
    b.addEventListener('click', function () { elegirSalida(b.dataset.salida, true); });
    b.addEventListener('keydown', function (ev) {
      var d = {ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1}[ev.key];
      if (d) {
        ev.preventDefault();
        elegirSalida(ops[(k + d + ops.length) % ops.length].dataset.salida, true);
      } else if (ev.key === 'Enter' || ev.key === ' ') {
        ev.preventDefault();
        elegirSalida(b.dataset.salida, false);
        siguienteCampo();
      }
    });
  });
  function irASalida() {
    lineas.cerrar();
    pintarDrop();
    var op = ops.filter(function (b) { return b.dataset.salida === salidaActual(); })[0] || ops[0];
    if (op) { op.focus(); }
  }
  // after the exit: its first empty field, else the button
  function siguienteCampo() {
    var visibles = Array.prototype.filter.call(form.querySelectorAll('input:not([type=hidden]),select'),
      function (el) { return el.offsetParent !== null && !el.disabled; });
    var vacio = visibles.filter(function (el) { return el.type !== 'checkbox' && !el.value; })[0];
    (vacio || $('pl-cargar')).focus();
  }
  $('pl-encomienda').addEventListener('change', pintarSalida);
  form.addEventListener('input', guardarBorrador);
  form.addEventListener('change', guardarBorrador);
  // Enter in a field of the exit goes to the next one (not a submit)
  form.addEventListener('keydown', function (ev) {
    if (ev.key !== 'Enter' || ev.ctrlKey || ev.metaKey || ev.target.tagName !== 'INPUT') { return; }
    ev.preventDefault();
    var visibles = Array.prototype.filter.call(form.querySelectorAll('input:not([type=hidden]),select,button[type=submit]'),
      function (el) { return el.offsetParent !== null && !el.disabled; });
    var k = visibles.indexOf(ev.target);
    if (k !== -1 && visibles[k + 1]) { visibles[k + 1].focus(); }
  });

  var hoy = new Date();
  var iso = hoy.getFullYear() + '-' + String(hoy.getMonth() + 1).padStart(2, '0') + '-' +
    String(hoy.getDate()).padStart(2, '0');
  $('pl-fecha').value = iso;
  $('pl-fecha').max = iso;

  function faltaWhatsapp() {
    if ($('pl-wa').hidden || body.getAttribute('data-wa-requerido') !== '1') { return false; }
    return !($('pl-wa-area').value.trim() || $('pl-wa-numero').value.trim());
  }

  // ------------------------------------------------------------ «Cargar la venta»

  var cargando = false;
  function cargar() {
    if (cargando || venta) { return; }
    limpiarErrores();
    if (!cliente) { mostrarError('Elegí el cliente.', 'pl-err-cliente'); $('pl-q-cliente').focus(); return; }
    if (lineas.sueltas()) {
      mostrarError('Hay un renglón con un producto que no está en la lista: elegilo con Enter o borralo.',
        'pl-err-lineas');
      return;
    }
    if (!lineas.renglones().length) { mostrarError('Agregá al menos un producto.', 'pl-err-lineas'); enfocarProducto(); return; }
    if (PRECIOS && lineas.preciosMalos()) {
      mostrarError('Revisá los precios: cada uno tiene que ser un número mayor que cero.', 'pl-err-lineas');
      return;
    }
    if (!salidaActual()) { mostrarError('Elegí cómo sigue la venta (Alt+1 a Alt+' + ops.length + ').', 'pl-err-cargar'); irASalida(); return; }
    if (salidaActual() === 'entregado' && !$('pl-motivo').value.trim()) {
      mostrarError('Poné el motivo de la entrega fuera del sistema.', 'pl-err-cargar');
      $('pl-motivo').focus();
      return;
    }
    if (faltaWhatsapp()) {
      mostrarError('El cliente no tiene WhatsApp y hace falta para confirmar: poné el código de área y el número.',
        'pl-err-cargar');
      $('pl-wa-area').focus();
      return;
    }
    if (!envio) { envio = nuevoEnvio(); }
    $('pl-envio').value = envio;
    pintarTotales();
    guardarBorrador();
    cargando = true;
    var btn = $('pl-cargar');
    btn.disabled = true;
    btn.classList.add('pl-esperando');
    pedir('/nueva-venta/crear', new FormData(form), 90000).then(function (r) {
      cargando = false;
      btn.classList.remove('pl-esperando');
      if (!r.data.ok || !r.data.order_id) {
        btn.disabled = false;
        mostrarError(r.data.error || 'No se pudo cargar la venta.', 'pl-err-cargar');
        return;
      }
      // kept at once: a reload goes on with this sale, never loads it twice
      pendiente = r.data.order_id;
      guardarBorrador();
      return abrirVenta(r.data.order_id, true);
    });
  }
  form.addEventListener('submit', function (ev) { ev.preventDefault(); cargar(); });

  // ------------------------------------------------------------ after the sale: delivery, payment, summary

  function abrirVenta(id, recien) {
    return pedir('/nueva-venta/planilla/venta/' + id).then(function (r) {
      if (!r.data.ok) {
        // the sale exists; only its summary failed: say so, keep the draft
        mostrarError((r.data.error || 'No se pudo leer la venta.') + ' La venta quedó cargada: recargá la página.',
          'pl-err-cargar');
        return;
      }
      mostrarVenta(r.data, recien);
    });
  }

  function mostrarVenta(v, recien) {
    venta = v;
    guardarBorrador();
    body.classList.add('pl--cargada');
    $('pl-corregida').hidden = true;
    $('pl-card-salida').hidden = true;
    $('pl-cambiar').hidden = true;
    $('pl-anteriores').hidden = true;
    pintarFilas();
    // the summary
    $('pl-card-hecho').hidden = false;
    $('pl-t-hecho').textContent = v.titulo;
    $('pl-h-texto').textContent = v.texto;
    $('pl-h-nombre').textContent = v.nombre;
    $('pl-h-cliente').textContent = v.cliente;
    $('pl-h-total').textContent = v.total_texto;
    $('pl-h-docs').innerHTML = v.documentos.map(function (d) {
      return '<li><span class="pl-docs__t">' + h(d.tipo) + '</span> <a href="' + h(d.url) +
        '" target="_blank" rel="noopener">' + h(d.nombre) + ' ↗</a></li>';
    }).join('');
    $('pl-h-pagos').innerHTML = v.pagos.map(function (p) { return '<li>' + h(p) + '</li>'; }).join('');
    $('pl-h-pagos').hidden = !v.pagos.length;
    $('pl-h-enviar').hidden = !v.enviar;
    if (v.enviar) { $('pl-h-enviar').href = v.enviar; }
    pintarPasos();
    // what the invoice still owes once delivered
    var saldo = $('pl-h-saldo');
    saldo.hidden = !(v.facturada && v.entregado && v.estado !== 'cancel');
    if (!saldo.hidden) {
      var falta = Math.max((v.residual || 0) - (v.en_camino || 0), 0);
      if (!(v.residual > 0.005)) {
        saldo.textContent = 'Pagada: no queda nada pendiente de esta venta.';
        saldo.className = 'pl-h-saldo pl-h-saldo--ok';
      } else if (falta <= 0.005) {
        saldo.textContent = 'Transferencia de ' + v.en_camino_texto + ' a verificar: se acredita cuando se revisa el comprobante.';
        saldo.className = 'pl-h-saldo pl-h-saldo--debe';
      } else {
        saldo.textContent = 'Queda en su cuenta corriente: ' + plata(falta) + '.';
        saldo.className = 'pl-h-saldo pl-h-saldo--debe';
      }
    }
    $('pl-h-remito').hidden = !v.remito || v.estado === 'cancel';
    avisoRemito('');
    // «Corregir la venta» while the counter sale waits for its delivery
    var corr = v.entrega && v.corregir;
    $('pl-corregir').hidden = !corr;
    $('pl-corregir-pregunta').hidden = true;
    if (corr) {
      $('pl-corregir-btn').disabled = !!v.corregir.motivo;
      $('pl-corregir-txt').textContent = v.corregir.motivo
        ? 'Esta venta no se puede corregir desde acá: ' + v.corregir.motivo
        : '¿Agrega o saca algo? La venta ya está facturada, así que no se le cambian los renglones: se anula ' +
          'con su nota de crédito y la cargás de nuevo con los cambios.';
    }
    // the delivery (Mostrador)
    var entrega = !!v.entrega;
    $('pl-card-entrega').hidden = !entrega;
    if (entrega) { prepararEntrega(v); }
    // «Cobrar» once nothing else is pending
    $('pl-card-cobrar').hidden = entrega || !v.cobrar;
    if (!entrega && v.cobrar) { prepararCobro(v); }
    if (recien || entrega || v.cobrar) {
      if (entrega) { $('pl-efectivo').focus(); } else if (v.cobrar && !v.a_cuenta) { $('pl-c-efectivo').focus(); } else { $('pl-nueva').focus(); }
    }
  }

  // ----- delivery

  function prepararEntrega(v) {
    var e = v.entrega;
    $('pl-entrega-venta').textContent = v.nombre;
    $('pl-e-pedido').textContent = plata(e.pedido);
    $('pl-e-deuda').textContent = plata(e.deuda);
    $('pl-bultos').value = e.bultos || '';
    $('pl-dev-filas').innerHTML = e.lineas.map(function (l) {
      return '<div class="pl-dev__fila"><span class="pl-dev__n">' + h(l.nombre) + '<small>Pidió ' + h(l.cantidad) + ' ' +
        h(l.unidad) + ' · ' + plata(l.unitario) + ' c/u</small></span>' +
        '<input class="dn-input dn-num" name="dev_' + l.id + '" data-unit="' + l.unitario + '" inputmode="decimal" ' +
        'placeholder="0" autocomplete="off" aria-label="Cantidad devuelta de ' + h(l.nombre) + '"/>' +
        '<input class="dn-input" name="motivo_' + l.id + '" maxlength="120" placeholder="Motivo: vencido, roto…" ' +
        'autocomplete="off" aria-label="Motivo de la devolución de ' + h(l.nombre) + '"/></div>';
    }).join('');
    if (!$('pl-efectivo').value && modoPago === 'efectivo') { $('pl-efectivo').value = montoCampo(e.pedido); }
    pintarPago();
  }
  function devolucion() {
    var total = 0;
    Array.prototype.forEach.call(document.querySelectorAll('#pl-dev-filas input[data-unit]'), function (el) {
      total += parseMonto(el.value) * (+el.dataset.unit || 0);
    });
    return $('pl-dev').hidden ? 0 : total;
  }
  var modoPago = 'efectivo';
  function pintarPago() {
    if (!venta || !venta.entrega) { return; }
    var e = venta.entrega;
    var dev = devolucion();
    var saldo = e.pedido + e.deuda - dev;
    $('pl-e-devolucion').textContent = plata(dev);
    $('pl-e-saldo').textContent = plata(saldo);
    Array.prototype.forEach.call(document.querySelectorAll('#pl-pago [data-pago]'), function (b) {
      b.setAttribute('aria-checked', b.dataset.pago === modoPago ? 'true' : 'false');
      b.tabIndex = b.dataset.pago === modoPago ? 0 : -1;
    });
    var conEfectivo = modoPago === 'efectivo' || modoPago === 'mixto';
    var conTransf = modoPago === 'transferencia' || modoPago === 'mixto';
    $('pl-efectivo-box').hidden = !conEfectivo;
    $('pl-transf-box').hidden = !conTransf;
    $('pl-transferencia').value = conTransf ? 'yes' : 'no';
    $('pl-no-pago').value = modoPago === 'nada' ? '1' : '';
    var subir = (document.querySelector('#pl-entrega-form input[name=comprobante]:checked') || {}).value !== 'despues';
    $('pl-zona').hidden = !subir;
    $('pl-archivos-lista').hidden = !subir;
    $('pl-despues-aviso').hidden = subir;
    var efectivo = conEfectivo ? parseMonto($('pl-efectivo').value) : 0;
    var transfer = conTransf ? parseMonto($('pl-importe-transf').value) : 0;
    var paga = parseMonto($('pl-paga-con').value);
    $('pl-vuelto').textContent = paga > efectivo && efectivo > 0 ? 'Vuelto: ' + plata(paga - efectivo) :
      (paga && paga < efectivo ? 'Con ' + plata(paga) + ' no alcanza para ' + plata(efectivo) + '.' : '');
    $('pl-debe-monto').textContent = plata(Math.max(saldo - efectivo - transfer, 0));
    $('pl-cc-aviso').hidden = modoPago !== 'nada';
    $('pl-debe-l').textContent = modoPago === 'nada' ? 'Queda en cuenta corriente' : 'Queda debiendo';
    $('pl-entregar-t').textContent = modoPago === 'nada' ? 'Entregar · a cuenta corriente' : 'Entregar y cobrar';
  }
  Array.prototype.forEach.call(document.querySelectorAll('#pl-pago [data-pago]'), function (b, k, all) {
    b.addEventListener('click', function () { elegirPago(b.dataset.pago); });
    b.addEventListener('keydown', function (ev) {
      var d = {ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1}[ev.key];
      if (!d) { return; }
      ev.preventDefault();
      var next = all[(k + d + all.length) % all.length];
      elegirPago(next.dataset.pago);
      next.focus();
    });
  });
  function elegirPago(modo) {
    if (!venta || !venta.entrega) { return; }
    modoPago = modo;
    var pedido = Math.max(venta.entrega.pedido - devolucion(), 0);
    if (modo === 'efectivo') { $('pl-efectivo').value = montoCampo(pedido); $('pl-importe-transf').value = ''; }
    if (modo === 'transferencia') { $('pl-importe-transf').value = montoCampo(pedido); $('pl-efectivo').value = ''; }
    if (modo === 'nada') { $('pl-efectivo').value = ''; $('pl-importe-transf').value = ''; }
    pintarPago();
  }
  $('pl-entrega-form').addEventListener('input', pintarPago);
  $('pl-entrega-form').addEventListener('change', function (ev) {
    if (ev.target.name === 'devolvio') { $('pl-dev').hidden = ev.target.value !== 'yes'; }
    pintarPago();
  });

  function problemaArchivo(file) {
    if (file.size > RECEIPT_MAX_BYTES) { return 'Es demasiado grande (más de 10 MB).'; }
    if (!RECEIPT_TYPES.test(file.type || '') && !RECEIPT_NAMES.test(file.name || '')) {
      return 'No es una foto (JPG, PNG o WEBP) ni un PDF.';
    }
    return '';
  }
  function kb(n) { return n > 1048576 ? (n / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(n / 1024)) + ' KB'; }
  function pintarArchivos() {
    $('pl-archivos-lista').innerHTML = archivos.map(function (f, i) {
      var mal = problemaArchivo(f);
      return '<li' + (mal ? ' class="pl-mal"' : '') + '><span>' + h(f.name) + ' <small>' + kb(f.size) + '</small>' +
        (mal ? ' <b>' + h(mal) + '</b>' : '') + '</span><button type="button" class="pl-x" data-quitar="' + i +
        '" aria-label="Quitar ' + h(f.name) + '">×</button></li>';
    }).join('');
  }
  function zona(zonaEl, input, agregar) {
    zonaEl.addEventListener('click', function () { input.click(); });
    zonaEl.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); input.click(); }
    });
    input.addEventListener('change', function () { agregar(Array.prototype.slice.call(input.files || [])); input.value = ''; });
    ['dragenter', 'dragover'].forEach(function (t) {
      zonaEl.addEventListener(t, function (ev) { ev.preventDefault(); zonaEl.classList.add('pl-sobre'); });
    });
    ['dragleave', 'drop'].forEach(function (t) {
      zonaEl.addEventListener(t, function () { zonaEl.classList.remove('pl-sobre'); });
    });
    zonaEl.addEventListener('drop', function (ev) {
      ev.preventDefault();
      agregar(Array.prototype.slice.call((ev.dataTransfer && ev.dataTransfer.files) || []));
    });
  }
  zona($('pl-zona'), $('pl-archivos'), function (files) {
    archivos = archivos.concat(files).slice(0, 10);
    pintarArchivos();
  });
  $('pl-archivos-lista').addEventListener('click', function (ev) {
    var b = ev.target.closest('button[data-quitar]');
    if (b) { archivos.splice(+b.dataset.quitar, 1); pintarArchivos(); }
  });
  // a file dropped anywhere on the page does not open it in the browser
  ['dragover', 'drop'].forEach(function (t) {
    window.addEventListener(t, function (ev) { if (!ev.target.closest || !ev.target.closest('.pl-drop-zone')) { ev.preventDefault(); } });
  });

  var entregando = false;
  function entregar() {
    if (entregando || !venta || !venta.entrega) { return; }
    limpiarErrores();
    var conTransf = $('pl-transferencia').value === 'yes';
    var subir = (document.querySelector('#pl-entrega-form input[name=comprobante]:checked') || {}).value !== 'despues';
    if (conTransf && subir) {
      var malos = archivos.filter(problemaArchivo);
      if (!archivos.length) { mostrarError('Subí el comprobante (foto o PDF) o elegí «Lo manda después».', 'pl-err-entrega'); return; }
      if (malos.length) { mostrarError('Revisá estos archivos: ' + malos.map(function (f) { return f.name; }).join(', ') + '.', 'pl-err-entrega'); return; }
    }
    var fd = new FormData($('pl-entrega-form'));
    if (!conTransf) { fd.delete('comprobante'); fd.delete('importe_transf'); }
    if (conTransf && subir) { archivos.forEach(function (f) { fd.append('comprobantes', f, f.name); }); }
    if ($('pl-dev').hidden) {
      Array.prototype.forEach.call(document.querySelectorAll('#pl-dev-filas input'), function (el) { fd.delete(el.name); });
    }
    entregando = true;
    var btn = $('pl-entregar');
    btn.disabled = true;
    btn.classList.add('pl-esperando');
    pedir('/deposito/entrega/' + venta.id + '?mostrador=1', fd, 90000).then(function (r) {
      entregando = false;
      btn.disabled = false;
      btn.classList.remove('pl-esperando');
      if (!r.data.ok) { mostrarError(r.data.error || 'No se pudo registrar la entrega.', 'pl-err-entrega'); return; }
      archivos = [];
      pintarArchivos();
      var msg = r.data.message || 'Entregado.';
      return abrirVenta(venta.id).then(function () { ok(msg); });
    });
  }
  $('pl-entrega-form').addEventListener('submit', function (ev) { ev.preventDefault(); entregar(); });

  function ok(texto) {
    $('pl-h-ok').textContent = texto;
    $('pl-h-ok').hidden = !texto;
  }

  // ----- «Cobrar»

  function prepararCobro(v) {
    $('pl-c-debe').textContent = '· debe ' + v.cobrar.debe_texto;
    // delivered «a cuenta corriente»: nothing was paid, so no amount is
    // offered (a second Ctrl+Enter must not collect by itself)
    if (!v.a_cuenta && !$('pl-c-efectivo').value && !transf.length) { $('pl-c-efectivo').value = montoCampo(v.cobrar.debe); }
    $('pl-c-destino').querySelector('option[value=factura]').disabled = !v.cobrar.factura_id;
    pintarTransf();
  }
  function pintarTransf() {
    $('pl-c-transf').innerHTML = transf.map(function (t, i) {
      var mal = t.file && problemaArchivo(t.file);
      return '<div class="pl-transf' + (mal ? ' pl-mal' : '') + '"><span class="pl-transf__n">' + h(t.nombre) +
        (t.recibo ? ' <small>ya subido</small>' : '') + (mal ? ' <b>' + h(mal) + '</b>' : '') + '</span>' +
        '<input class="dn-input dn-num" data-importe="' + i + '" inputmode="decimal" placeholder="Importe" value="' +
        h(t.importe) + '" aria-label="Importe de la transferencia ' + (i + 1) + '"/>' +
        '<button type="button" class="pl-x" data-quitar="' + i + '" aria-label="Quitar ' + h(t.nombre) + '">×</button></div>';
    }).join('');
  }
  zona($('pl-c-zona'), $('pl-c-archivos'), function (files) {
    files.forEach(function (f) { if (transf.length < MAX_TRANSF) { transf.push({file: f, nombre: f.name, importe: '', recibo: null}); } });
    if (transf.length === files.length && transf.length === 1 && venta && venta.cobrar &&
        parseMonto($('pl-c-efectivo').value) >= venta.cobrar.debe) {
      // one receipt for the whole debt: the cash prefilled before goes away
      transf[0].importe = montoCampo(venta.cobrar.debe);
      $('pl-c-efectivo').value = '';
    }
    pintarTransf();
    var first = $('pl-c-transf').querySelector('input[data-importe=""], input[data-importe]');
    if (first) { first.focus(); }
  });
  $('pl-c-transf').addEventListener('input', function (ev) {
    var i = ev.target.dataset.importe;
    if (i != null) { transf[+i].importe = ev.target.value; }
  });
  $('pl-c-transf').addEventListener('click', function (ev) {
    var b = ev.target.closest('button[data-quitar]');
    if (b) { transf.splice(+b.dataset.quitar, 1); pintarTransf(); }
  });
  $('pl-cobro-form').addEventListener('input', function (ev) {
    if (ev.target.id !== 'pl-c-efectivo' && !ev.target.dataset.importe) { return; }
    flagsCobro = {};
    $('pl-c-pregunta').hidden = true;
  });

  var cobrando = false;
  function cobrar() {
    if (cobrando || !venta || !venta.cobrar) { return; }
    limpiarErrores();
    var fd = new FormData($('pl-cobro-form'));
    transf.forEach(function (t, i) {
      var n = i + 1;
      fd.append('importe_transf_' + n, t.importe || '');
      if (t.recibo) { fd.append('recibo_' + n, t.recibo); } else if (t.file) { fd.append('comprobante_' + n, t.file, t.file.name); }
    });
    Object.keys(flagsCobro).forEach(function (k) { fd.append(k, '1'); });
    cobrando = true;
    var btn = $('pl-cobrar');
    btn.disabled = true;
    btn.classList.add('pl-esperando');
    pedir('/nueva-venta/planilla/cobrar/' + venta.id, fd, 90000).then(function (r) {
      cobrando = false;
      btn.disabled = false;
      btn.classList.remove('pl-esperando');
      var d = r.data;
      (d.recibos || []).forEach(function (x) {
        if (transf[x.n - 1]) { transf[x.n - 1].recibo = x.receipt_id; transf[x.n - 1].file = null; }
      });
      if (d.recibos) { pintarTransf(); }
      if (r.status === 409 && d.pregunta) {
        $('pl-c-pregunta-txt').textContent = d.pregunta;
        $('pl-c-pregunta').hidden = false;
        $('pl-c-si').dataset.campo = d.campo || '';
        $('pl-c-si').focus();
        return;
      }
      if (!d.ok) { mostrarError(d.error || 'No se pudo cobrar.', 'pl-err-cobro'); return; }
      transf = [];
      flagsCobro = {};
      $('pl-c-efectivo').value = '';
      $('pl-c-nota').value = '';
      pintarTransf();
      mostrarVenta(d.venta, false);
      ok(d.texto);
      $('pl-nueva').focus();
    });
  }
  $('pl-c-si').addEventListener('click', function () {
    var campo = $('pl-c-si').dataset.campo;
    if (campo) { flagsCobro[campo] = true; }
    $('pl-c-pregunta').hidden = true;
    cobrar();
  });
  $('pl-c-no').addEventListener('click', function () { $('pl-c-pregunta').hidden = true; $('pl-c-efectivo').focus(); });
  $('pl-cobro-form').addEventListener('submit', function (ev) { ev.preventDefault(); cobrar(); });

  // ------------------------------------------------------------ next sale

  // back to an empty sale on the same screen (the customer stays chosen)
  function limpiarVenta() {
    quitar(KEY);
    venta = null;
    pendiente = null;
    envio = '';
    archivos = [];
    transf = [];
    flagsCobro = {};
    modoPago = 'efectivo';
    body.classList.remove('pl--cargada');
    ['pl-entrega-form', 'pl-cobro-form'].forEach(function (id) { $(id).reset(); });
    $('pl-dev').hidden = true;
    pintarArchivos();
    pintarTransf();
    ['pl-card-entrega', 'pl-card-cobrar', 'pl-card-hecho', 'pl-corregir', 'pl-corregida'].forEach(function (id) {
      $(id).hidden = true;
    });
    ok('');
    $('pl-card-salida').hidden = false;
    $('pl-cambiar').hidden = false;
    limpiarErrores();
    pintarPasos();
  }

  function reiniciar(mismoCliente) {
    if (venta && venta.entrega && !window.confirm('La entrega de ' + venta.nombre + ' queda pendiente en Depósito. ¿Empezar otra venta igual?')) {
      return;
    }
    limpiarVenta();
    ['comentario', 'nota_deposito', 'contacto', 'enc_nombre', 'enc_dni', 'enc_localidad'].forEach(function (n) {
      if (form.elements[n]) { form.elements[n].value = ''; }
    });
    $('pl-encomienda').checked = false;
    $('pl-motivo').value = $('pl-motivo').defaultValue;
    $('pl-salida').value = '';
    lineas.vaciar();
    if (mismoCliente && cliente) {
      // the same customer: his catalog and data are still here
      pintarCliente();
      pintarAnteriores(anteriores);
      pintarFilas();
      pintarSalida();
      enfocarProducto(0);
      guardarBorrador();
      refrescarCliente();
      return;
    }
    cliente = null;
    $('pl-cliente-id').value = '';
    ['telefono', 'domicilio', 'wa_area', 'wa_numero'].forEach(function (n) { form.elements[n].value = ''; });
    lineas.setCatalogo([]);
    $('pl-elegido').hidden = true;
    $('pl-anteriores').hidden = true;
    $('pl-buscar-cliente').hidden = false;
    $('pl-tabla').hidden = true;
    $('pl-sin-cliente').hidden = false;
    $('pl-precios-de').textContent = '';
    $('pl-q-cliente').value = '';
    pintarFilas();
    pintarSalida();
    $('pl-q-cliente').focus();
  }
  // after a sale to the same customer: his balance and last orders again
  // (the lines being typed are not touched)
  function refrescarCliente() {
    var id = cliente && cliente.id;
    if (!id) { return; }
    pedir('/nueva-venta/catalogo?cliente_id=' + encodeURIComponent(id)).then(function (r) {
      if (!r.data.ok || !cliente || cliente.id !== id || venta) { return; }
      cliente.saldo = r.data.cliente.saldo;
      cliente.saldo_texto = r.data.cliente.saldo_texto;
      pintarCliente();
      pintarAnteriores(r.data.anteriores || []);
    });
  }

  // ------------------------------------------------------------ «Corregir la venta» (before the delivery)

  function pedirCorregir() {
    if (!venta || !venta.entrega || !venta.corregir || venta.corregir.motivo) { return; }
    $('pl-corregir-nombre').textContent = venta.nombre;
    $('pl-corregir-pasos').innerHTML = (venta.corregir.pasos || []).map(function (p) { return '<li>' + h(p) + '</li>'; }).join('');
    $('pl-corregir-pregunta').hidden = false;
    $('pl-corregir-si').focus();
    if ($('pl-corregir-pregunta').scrollIntoView) { $('pl-corregir-pregunta').scrollIntoView({block: 'nearest'}); }
  }
  var corrigiendo = false;
  function corregir() {
    if (corrigiendo || !venta || !venta.entrega) { return; }
    corrigiendo = true;
    var btn = $('pl-corregir-si');
    btn.disabled = true;
    btn.classList.add('pl-esperando');
    $('pl-err-corregir').hidden = true;
    pedir('/nueva-venta/planilla/corregir/' + venta.id, new FormData(), 90000).then(function (r) {
      corrigiendo = false;
      btn.disabled = false;
      btn.classList.remove('pl-esperando');
      if (!r.data.ok) {
        $('pl-err-corregir').textContent = r.data.error || 'No se pudo corregir la venta.';
        $('pl-err-corregir').hidden = false;
        return;
      }
      reabrir(r.data.renglones || [], r.data.texto || '');
    });
  }
  // the cancelled sale's customer and lines, to change and load as a new sale
  function reabrir(renglones, texto) {
    limpiarVenta();
    lineas.cargar(renglones);
    var faltan = renglones.length - lineas.renglones().length;
    if (ops.some(function (b) { return b.dataset.salida === 'mostrador'; })) { $('pl-salida').value = 'mostrador'; }
    pintarCliente();
    pintarAnteriores(anteriores);
    pintarFilas();
    pintarSalida();
    $('pl-corregida').textContent = texto;
    $('pl-corregida').hidden = !texto;
    if (faltan > 0) {
      mostrarError(faltan === 1 ? 'Un producto de la venta anulada ya no está en el catálogo: agregalo de nuevo si corresponde.'
        : faltan + ' productos de la venta anulada ya no están en el catálogo: agregalos de nuevo si corresponde.', 'pl-err-lineas');
    }
    guardarBorrador();
    enfocarProducto();
    refrescarCliente();
  }
  $('pl-corregir-btn').addEventListener('click', pedirCorregir);
  $('pl-corregir-si').addEventListener('click', corregir);
  $('pl-corregir-no').addEventListener('click', function () {
    $('pl-corregir-pregunta').hidden = true;
    if (!$('pl-card-entrega').hidden) { $('pl-efectivo').focus(); }
  });

  // ------------------------------------------------------------ the remito: print or share

  function avisoRemito(texto) {
    $('pl-h-remito-aviso').textContent = texto;
    $('pl-h-remito-aviso').hidden = !texto;
  }
  function bajarRemito() {
    return fetch(venta.remito.url, {credentials: 'same-origin'}).then(function (r) {
      if (!r.ok) { throw new Error('remito'); }
      return r.blob();
    });
  }
  var marcoRemito = null;   // the hidden frame the remito is printed from
  function abrirRemito() { window.open(venta.remito.url, '_blank', 'noopener'); }
  function imprimirRemito() {
    if (!venta || !venta.remito) { return; }
    avisoRemito('Preparando el remito para imprimir…');
    bajarRemito().then(function (blob) {
      var marco = marcoRemito;
      if (!marco) {
        marco = marcoRemito = document.createElement('iframe');
        marco.setAttribute('aria-hidden', 'true');
        marco.style.cssText = 'position:fixed;right:0;bottom:0;width:0;height:0;border:0;visibility:hidden';
        document.body.appendChild(marco);
      }
      marco.onload = function () {
        setTimeout(function () {
          try {
            marco.contentWindow.focus();
            marco.contentWindow.print();
            avisoRemito('Si no se abrió la impresión, abrí el remito desde el enlace de arriba.');
          } catch (e) { abrirRemito(); avisoRemito(''); }
        }, 300);
      };
      marco.src = URL.createObjectURL(new Blob([blob], {type: 'application/pdf'}));
    }, function () { abrirRemito(); avisoRemito(''); });
  }
  function descargarRemito(blob) {
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = venta.remito.archivo;
    document.body.appendChild(a);
    a.click();
    a.remove();
    avisoRemito('Se descargó «' + venta.remito.archivo + '»: arrastralo al chat de WhatsApp del cliente.');
  }
  function compartirRemito() {
    if (!venta || !venta.remito) { return; }
    avisoRemito('Preparando el remito…');
    bajarRemito().then(function (blob) {
      var file = null;
      try { file = new File([blob], venta.remito.archivo, {type: 'application/pdf'}); } catch (e) { /* old browser */ }
      if (file && navigator.canShare && navigator.canShare({files: [file]})) {
        avisoRemito('');
        return navigator.share({files: [file], title: 'Remito ' + venta.remito.nombre}).catch(function (err) {
          // closed by the user: nothing; refused by the browser: the file instead
          if (!err || err.name !== 'AbortError') { descargarRemito(blob); }
        });
      }
      descargarRemito(blob);
    }, function () { abrirRemito(); avisoRemito(''); });
  }
  $('pl-imprimir').addEventListener('click', imprimirRemito);
  $('pl-compartir').addEventListener('click', compartirRemito);

  $('pl-nueva').addEventListener('click', function () { reiniciar(false); });
  $('pl-otra').addEventListener('click', function () { reiniciar(true); });

  // ------------------------------------------------------------ shortcuts

  document.addEventListener('keydown', function (ev) {
    var a = atajo(ev);
    if (!a) { return; }
    if (a === 'cargar') {
      ev.preventDefault();
      if (!venta) { cargar(); } else if (!$('pl-card-entrega').hidden) { entregar(); } else if (!$('pl-card-cobrar').hidden) { cobrar(); }
      return;
    }
    if (a.indexOf('salida:') === 0) {
      var op = ops[+a.slice(7) - 1];
      if (op && !venta) { ev.preventDefault(); elegirSalida(op.dataset.salida, false); siguienteCampo(); }
      return;
    }
    if (a === 'cliente') { ev.preventDefault(); if (!venta) { cliente ? cambiarCliente() : $('pl-q-cliente').focus(); } return; }
    if (a === 'productos') { ev.preventDefault(); enfocarProducto(); return; }
    if (a === 'nueva' && venta) { ev.preventDefault(); reiniciar(false); return; }
    if (a === 'corregir' && venta && venta.entrega && venta.corregir) { ev.preventDefault(); pedirCorregir(); return; }
    if (a === 'remito' && venta && venta.remito && !$('pl-h-remito').hidden) { ev.preventDefault(); imprimirRemito(); }
  });

  // ------------------------------------------------------------ start

  function restaurar(b) {
    restaurando = true;
    if (PRECIOS) { Object.keys(b.extras || {}).forEach(function (id) { lineas.agregarExtra(b.extras[id]); }); }
    lineas.cargar(b.renglones || []);
    Object.keys(b.campos || {}).forEach(function (n) {
      var el = form.elements[n];
      if (el && b.campos[n] != null) { el.value = b.campos[n]; }
    });
    $('pl-encomienda').checked = !!b.encomienda;
    $('pl-salida').value = b.salida || '';
    envio = b.envio || '';
    restaurando = false;
  }
  var borrador = leerBorrador();
  pintarSalida();
  pintarPasos();
  if (borrador && borrador.venta_id) {
    // a sale already loaded: go on with its delivery / payment / summary
    pendiente = borrador.venta_id;
    elegirCliente(borrador.cliente, borrador).then(function (ok2) { if (ok2) { abrirVenta(borrador.venta_id, false); } });
  } else if (borrador) {
    var n = (borrador.renglones || []).length;
    $('pl-borrador-txt').textContent = 'Tenés una venta sin terminar: ' + borrador.cliente.nombre +
      (n ? ', ' + n + (n === 1 ? ' producto' : ' productos') : '') + '.';
    $('pl-borrador').hidden = false;
    $('pl-borrador-seguir').addEventListener('click', function () {
      $('pl-borrador').hidden = true;
      elegirCliente(borrador.cliente, borrador);
    });
    $('pl-borrador-descartar').addEventListener('click', function () {
      $('pl-borrador').hidden = true;
      quitar(KEY);
      $('pl-q-cliente').focus();
    });
    $('pl-borrador-seguir').focus();
  } else {
    $('pl-q-cliente').focus();
  }
}());
