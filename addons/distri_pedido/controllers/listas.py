"""19.0.1.129.52 (owner, 26/11/2026): «Listas y accesos» (/listas), only for
the group «Listas y accesos de distribuidores» (Gonzalo and Ale). Everybody
else gets 403, whatever his role; the model methods check the group again
(models/listas_distri.py check_listas), so the screen is not the only lock.

- /listas: the customers, with search and a filter by category; each one
  opens /listas/cliente/<id>, where the category (1-6) is chosen, the change
  and the open orders are previewed, and «Aplicar» sets it (opt-in «Actualizar
  precios» per open order).
- /listas/cigarrillos: the cigarettes × Lista 2 / Lista 3 / Lista 4 grid; for
  Lista 3/4 the «May X - lista N» product price next to it and «Usar precio
  May» per row or for all. Empty = «a presupuestar».
  /listas/cigarrillos/vincular links each «May X - lista N» product to its
  cigarette.
- /listas/accesos: web access of the customers with a hidden list: create
  (login = CUIT digits, or a generated one), regenerate the password,
  deactivate. The temporary password is shown once, with a WhatsApp link.
- /listas/carga: the CSV initial load (partner id, category, list name for
  6), previewed and then applied.
"""
from werkzeug.exceptions import Forbidden, NotFound

from odoo import http
from odoo.exceptions import AccessError, UserError
from odoo.http import request

from ..models.listas_distri import (
    CATEGORIAS, LISTAS_CIGARRILLOS, NOMBRE_CATEGORIA, PERSONAL_POR_CLAVE, plata, puede_listas,
)
from .kit import kit_context, page_nav

PESTANAS = (("clientes", "Clientes", "/listas"), ("cigarrillos", "Cigarrillos", "/listas/cigarrillos"),
            ("accesos", "Accesos web", "/listas/accesos"), ("carga", "Carga inicial", "/listas/carga"))
NOMBRE_LISTA_GRILLA = {"lista2": "Lista 2", "lista3": "Lista 3", "lista4": "Lista 4"}


def _precio(texto):
    """A price typed in the grid («12.450», «12450,50», «12450.5»), None
    when empty. Raises UserError when it cannot be read."""
    texto = (texto or "").strip().replace("$", "").replace(" ", "")
    if not texto:
        return None
    if "," in texto:
        texto = texto.replace(".", "").replace(",", ".")
    elif texto.count(".") == 1 and len(texto.split(".")[1]) == 3:
        texto = texto.replace(".", "")  # «12.450»: thousands
    elif texto.count(".") > 1:
        texto = texto.replace(".", "")
    try:
        valor = float(texto)
    except ValueError:
        raise UserError("No se entiende el precio «%s»." % texto) from None
    if valor < 0:
        raise UserError("El precio no puede ser negativo.")
    return valor


def _num(valor):
    """A price for an input: «12450», «12450.5», «» for none."""
    if valor is None:
        return ""
    return "%d" % valor if float(valor).is_integer() else "%.2f" % valor


class ListasAccesos(http.Controller):

    def _check(self):
        if not puede_listas(request.env.user):
            raise Forbidden()
        return request.env["distri.listas"]

    def _page(self, template, pestana, values):
        user = request.env.user
        base = {
            "dn": kit_context("deposito.css", "listas.css"),
            "user": user,
            "inicio": "/tablero",
            "nav": page_nav(request.env, "listas"),
            "ok": "",
            "error": "",
            "csrf_token": request.csrf_token(),
            "pestanas": PESTANAS,
            "pestana": pestana,
            "plata": plata,
            "num": _num,
            "categorias": CATEGORIAS,
            "nombre_categoria": NOMBRE_CATEGORIA,
        }
        base.update(values)
        html = request.env["ir.qweb"]._render(template, base)
        return request.make_response(html, headers=[
            ("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store"),
            ("Referrer-Policy", "no-referrer")])

    # --------------------------------------------------------- customers

    @http.route("/listas", type="http", auth="user", methods=["GET"], sitemap=False)
    def clientes(self, q="", cat="", **kw):
        Listas = self._check()
        cat = cat if cat in NOMBRE_CATEGORIA else ""
        filas = Listas._clientes(q, cat)
        return self._page("distri_pedido.listas_clientes_page", "clientes", {
            "filas": filas[:200], "total": len(filas), "q": q, "cat": cat})

    def _socio(self, socio_id):
        socio = request.env["res.partner"].sudo().browse(socio_id).exists()
        if not socio or socio.parent_id or socio.distri_generic_partner:
            raise NotFound()
        return socio

    @http.route("/listas/cliente/<int:socio_id>", type="http", auth="user", methods=["GET"],
                sitemap=False)
    def cliente(self, socio_id, categoria="", lista="", hecho="", **kw):
        self._check()
        return self._cliente_page(self._socio(socio_id), categoria, lista, hecho)

    def _cliente_page(self, socio, categoria="", lista="", hecho="", error_msg=""):
        Listas = request.env["distri.listas"]
        cat_actual, lista_actual = Listas._categoria(socio)
        previa, error = None, error_msg
        if categoria and not error:
            try:
                previa = Listas._vista_previa(socio, categoria, lista or None)
            except UserError as exc:
                error = str(exc.args[0]) if exc.args else str(exc)
        return self._page("distri_pedido.listas_cliente_page", "clientes", {
            "socio": socio, "cat_actual": cat_actual, "lista_actual": lista_actual,
            "otras": Listas._listas_otra(), "previa": previa, "error": error,
            "categoria": categoria, "lista_elegida": lista,
            "pedidos_abiertos": Listas._pedidos_abiertos(socio),
            "ok": "Listo: categoría aplicada." if hecho else ""})

    @http.route("/listas/cliente/<int:socio_id>", type="http", auth="user", methods=["POST"],
                sitemap=False)
    def cliente_aplicar(self, socio_id, categoria="", lista="", **kw):
        Listas = self._check()
        socio = self._socio(socio_id)
        pedidos = request.httprequest.form.getlist("pedido")
        try:
            Listas._aplicar_categoria(socio, categoria, lista or None, pedidos,
                                      origen="Listas y accesos")
        except UserError as exc:
            request.env.cr.rollback()
            return self._cliente_page(socio, categoria, lista,
                                      error_msg=exc.args[0] if exc.args else str(exc))
        return request.redirect("/listas/cliente/%s?hecho=1" % socio.id, 303)

    # -------------------------------------------------- cigarette prices

    def _grilla_page(self, ok="", error=""):
        Listas = request.env["distri.listas"]
        grilla = Listas._grilla()
        return self._page("distri_pedido.listas_cigarrillos_page", "cigarrillos", {
            "filas": grilla["filas"], "listas": grilla["listas"], "claves": LISTAS_CIGARRILLOS,
            "nombre_lista": NOMBRE_LISTA_GRILLA, "con_may": PERSONAL_POR_CLAVE,
            "ok": ok, "error": error})

    @http.route("/listas/cigarrillos", type="http", auth="user", methods=["GET"], sitemap=False)
    def cigarrillos(self, hecho="", **kw):
        self._check()
        return self._grilla_page(ok=("Listo: %s precio(s) cambiado(s)." % hecho) if hecho else "")

    @http.route("/listas/cigarrillos", type="http", auth="user", methods=["POST"], sitemap=False)
    def cigarrillos_guardar(self, usar="", usar_todos="", **kw):
        """Saves every price that changed («p_<list>_<id>», compared with
        «o_<list>_<id>», the value the page showed), then a «usar precio May»
        of one row («usar=lista3:<id>») or of a whole list («usar_todos»)."""
        Listas = self._check()
        form = request.httprequest.form
        cambios = {}
        try:
            for nombre in form:
                if not nombre.startswith("p_"):
                    continue
                _p, clave, tmpl = nombre.split("_", 2)
                if clave not in LISTAS_CIGARRILLOS or not tmpl.isdigit():
                    continue
                nuevo, antes = _precio(form.get(nombre)), _precio(form.get("o_%s_%s" % (clave, tmpl)))
                if nuevo != antes:
                    cambios[(clave, int(tmpl))] = nuevo
            n = Listas._guardar_precios(cambios) if cambios else 0
            if usar:
                clave, _sep, tmpl = usar.partition(":")
                if tmpl.isdigit():
                    n += Listas._usar_precio_may(clave, {int(tmpl)})
            if usar_todos:
                n += Listas._usar_precio_may(usar_todos)
        except UserError as exc:
            request.env.cr.rollback()
            return self._grilla_page(error=exc.args[0] if exc.args else str(exc))
        return request.redirect("/listas/cigarrillos?hecho=%s" % n, 303)

    def _vincular_page(self, ok="", error=""):
        Listas = request.env["distri.listas"]
        Listas._grilla()  # the group check
        cigarrillos = Listas._cigarrillos()
        productos = request.env["product.template"].sudo().search(
            [("distri_lista_personal", "in", ("lista_3", "lista_4"))],
            order="distri_lista_personal, name, id")
        return self._page("distri_pedido.listas_vincular_page", "cigarrillos", {
            "cigarrillos": cigarrillos, "productos": productos, "ok": ok, "error": error})

    @http.route("/listas/cigarrillos/vincular", type="http", auth="user", methods=["GET"],
                sitemap=False)
    def vincular(self, hecho="", **kw):
        self._check()
        return self._vincular_page(ok=("Listo: %s producto(s) vinculado(s)." % hecho)
                                   if hecho else "")

    @http.route("/listas/cigarrillos/vincular", type="http", auth="user", methods=["POST"],
                sitemap=False)
    def vincular_guardar(self, **kw):
        """«v_<May product id>» = «#<cigarette id> name» from the datalist, or
        empty to unlink."""
        Listas = self._check()
        Tmpl = request.env["product.template"].sudo()
        form = request.httprequest.form
        n = 0
        try:
            pares, vistos = [], {}
            for nombre in form:
                if not nombre.startswith("v_") or not nombre[2:].isdigit():
                    continue
                producto = Tmpl.browse(int(nombre[2:])).exists()
                if not producto:
                    continue
                texto = (form.get(nombre) or "").strip()
                cig = Tmpl.browse()
                if texto:
                    numero = texto.lstrip("#").split(" ", 1)[0]
                    if not numero.isdigit():
                        raise UserError("Elegí el cigarrillo de la lista para «%s»." % producto.name)
                    cig = Tmpl.browse(int(numero)).exists()
                    clave = (producto.distri_lista_personal, cig.id)
                    if clave in vistos:
                        raise UserError("«%s» y «%s» apuntan al mismo cigarrillo: cada cigarrillo "
                                        "va con un solo producto May por lista."
                                        % (vistos[clave], producto.name))
                    vistos[clave] = producto.name
                pares.append((producto, cig))
            # unlink first, so a product can take the cigarette another one leaves
            for producto, cig in sorted(pares, key=lambda par: bool(par[1])):
                if Listas._vincular(producto, cig):
                    n += 1
        except UserError as exc:
            request.env.cr.rollback()
            return self._vincular_page(error=exc.args[0] if exc.args else str(exc))
        return request.redirect("/listas/cigarrillos/vincular?hecho=%s" % n, 303)

    # -------------------------------------------------------- web access

    @http.route("/listas/accesos", type="http", auth="user", methods=["GET"], sitemap=False)
    def accesos(self, **kw):
        Listas = self._check()
        return self._page("distri_pedido.listas_accesos_page", "accesos", {
            "filas": Listas._accesos(), "clave": None})

    @http.route("/listas/accesos/<int:socio_id>", type="http", auth="user", methods=["POST"],
                sitemap=False)
    def acceso_accion(self, socio_id, accion="", **kw):
        Listas = self._check()
        socio = self._socio(socio_id)
        resultado = None
        try:
            if accion == "crear":
                usuario, clave = socio.with_env(request.env)._distri_crear_acceso()
            elif accion == "clave":
                usuario, clave = socio.with_env(request.env)._distri_regenerar_clave()
            elif accion == "desactivar":
                socio.with_env(request.env)._distri_desactivar_acceso()
                usuario, clave = None, None
            else:
                raise UserError("Acción desconocida.")
        except (UserError, AccessError) as exc:
            request.env.cr.rollback()
            return self._page("distri_pedido.listas_accesos_page", "accesos", {
                "filas": Listas._accesos(), "clave": None,
                "error": exc.args[0] if exc.args else str(exc)})
        if clave:
            texto = socio._distri_texto_whatsapp(usuario.login, clave)
            resultado = {"socio": socio, "login": usuario.login, "clave": clave,
                         "whatsapp": socio._distri_link_whatsapp(texto), "texto": texto}
        return self._page("distri_pedido.listas_accesos_page", "accesos", {
            "filas": Listas._accesos(), "clave": resultado,
            "ok": "" if clave else "Acceso de %s desactivado." % socio.name})

    # ------------------------------------------------------ initial load

    @http.route("/listas/carga", type="http", auth="user", methods=["GET"], sitemap=False)
    def carga(self, **kw):
        self._check()
        return self._page("distri_pedido.listas_carga_page", "carga", {
            "filas": None, "contenido": ""})

    @http.route("/listas/carga", type="http", auth="user", methods=["POST"], sitemap=False)
    def carga_enviar(self, contenido="", confirmar="", **kw):
        """Upload → preview (nothing written); «Confirmar» posts the same
        content back (hidden field) and applies the valid rows."""
        Listas = self._check()
        archivo = request.httprequest.files.get("archivo")
        datos = archivo.read() if archivo else (contenido or "").encode("utf-8")
        try:
            if confirmar:
                aplicadas, filas = Listas._aplicar_csv(datos)
                return self._page("distri_pedido.listas_carga_page", "carga", {
                    "filas": filas, "contenido": "", "aplicado": True,
                    "ok": "Listo: %s cliente(s) cambiado(s)." % aplicadas})
            filas = Listas._leer_csv(datos)
        except UserError as exc:
            request.env.cr.rollback()
            return self._page("distri_pedido.listas_carga_page", "carga", {
                "filas": None, "contenido": "", "error": exc.args[0] if exc.args else str(exc)})
        try:
            texto = datos.decode("utf-8-sig")
        except UnicodeDecodeError:
            texto = datos.decode("latin-1")
        return self._page("distri_pedido.listas_carga_page", "carga", {
            "filas": filas, "contenido": texto, "aplicado": False,
            "a_aplicar": len([f for f in filas if f["estado"] == "ok"]),
            "errores": len([f for f in filas if f["estado"] == "error"])})
