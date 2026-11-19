#!/usr/bin/env python3
"""End-to-end check of the practice environment over HTTP (stdlib only).

    python3 -I probar.py [out.json]

Talks to http://127.0.0.1:8070 with Host: practica.distrineuquen.com, like
Caddy will. Checks the landing, the access code, every role button (lands on
its screen, banner present), one real action per role family, the banner on
the backend, and writes the session cookies of each role to out.json (for the
screenshots). Exit code 1 on any failure.
"""
import http.client
import json
import re
import sys
import urllib.parse

HOST = "practica.distrineuquen.com"
CODE = open("/srv/odoo-lab/practica/.codigo").read().strip()
BANNER = 'id="dn-practica-banner"'
EXPECTED = {
    "armador": "/armado", "repartidor": "/reparto", "repartidor2": "/reparto",
    "deposito": "/despacho", "cajero": "/caja", "administracion": "/tablero",
    "gerencia": "/tablero", "vendedor": "/pedido",
}
results, failures = [], []


def check(name, ok, detail=""):
    results.append({"check": name, "ok": bool(ok), "detail": detail})
    print(("OK   " if ok else "FAIL ") + name + (" · " + detail if detail else ""))
    if not ok:
        failures.append(name)


class Client:
    def __init__(self):
        self.cookies = {}

    def req(self, method, path, body=None, headers=None, follow=True, hops=0):
        conn = http.client.HTTPConnection("127.0.0.1", 8070, timeout=120)
        h = {"Host": HOST, "User-Agent": "practica-e2e"}
        if self.cookies:
            h["Cookie"] = "; ".join("%s=%s" % kv for kv in self.cookies.items())
        h.update(headers or {})
        conn.request(method, path, body=body, headers=h)
        r = conn.getresponse()
        data = r.read().decode("utf-8", "replace")
        for k, v in r.getheaders():
            if k.lower() == "set-cookie":
                name, _, rest = v.partition("=")
                self.cookies[name.strip()] = rest.split(";", 1)[0]
        loc = r.getheader("Location")
        if follow and r.status in (301, 302, 303, 307, 308) and loc and hops < 8:
            p = urllib.parse.urlsplit(loc)
            nxt = p.path + ("?" + p.query if p.query else "")
            return self.req("GET", nxt, follow=True, hops=hops + 1)
        return r.status, data, path

    def csrf(self, path="/"):
        _s, html, _p = self.req("GET", path)
        m = re.search(r'name="csrf_token" value="([^"]+)"', html) or \
            re.search(r'name="csrf-token" content="([^"]+)"', html)
        return m.group(1) if m else ""

    def post_form(self, path, fields, follow=True):
        body = urllib.parse.urlencode(fields)
        return self.req("POST", path, body=body, follow=follow,
                        headers={"Content-Type": "application/x-www-form-urlencoded"})

    def rpc(self, model, method, args, kwargs=None):
        body = json.dumps({"jsonrpc": "2.0", "method": "call", "id": 1, "params": {
            "model": model, "method": method, "args": args, "kwargs": kwargs or {}}})
        _s, data, _p = self.req("POST", "/web/dataset/call_kw/%s/%s" % (model, method), body=body,
                                headers={"Content-Type": "application/json"})
        return json.loads(data)


def enter(role):
    c = Client()
    token = c.csrf("/")
    status, html, path = c.post_form("/practica/entrar", {"csrf_token": token, "rol": role,
                                                          "codigo": CODE})
    return c, status, html, path


# ---------------------------------------------------------------- landing
c = Client()
status, html, _ = c.req("GET", "/")
check("landing 200", status == 200, str(status))
check("landing title", "Entorno de práctica de Distri Neuquén" in html)
check("landing banner", BANNER in html and "Se reinicia todas las noches" in html and "Cambiar de rol" in html)
buttons = re.findall(r'name="rol" value="([a-z0-9]+)"', html)
check("landing 7 role buttons", buttons == ["armador", "repartidor", "deposito", "cajero",
                                            "administracion", "gerencia", "vendedor"], ",".join(buttons))
token = c.csrf("/")
status, html, _ = c.post_form("/practica/entrar", {"csrf_token": token, "rol": "armador",
                                                   "codigo": "0000" if CODE != "0000" else "1111"})
check("wrong code refused", status == 403 and "no es correcto" in html, str(status))
status, html, _ = c.req("GET", "/armado")
check("still anonymous after wrong code", "/web/login" in html or status in (302, 303, 403) or "login" in html.lower())
# without CSRF token
status, _h, _ = Client().post_form("/practica/entrar", {"rol": "armador", "codigo": CODE}, follow=False)
check("POST without CSRF refused", status in (400, 403), str(status))

# ---------------------------------------------------------------- every role
sessions = {}
for role, url in EXPECTED.items():
    cl, status, html, path = enter(role)
    landed = path.split("?")[0]
    check("role %s lands on %s" % (role, url), status == 200 and landed.startswith(url),
          "%s %s" % (status, path))
    check("role %s banner" % role, BANNER in html)
    sessions[role] = {"session_id": cl.cookies.get("session_id"), "path": path}
    if role == "administracion":
        admin = cl
    if role == "cajero":
        cajero = cl
    if role == "repartidor":
        driver = cl
    if role == "deposito":
        depot = cl

# landing after entering: code remembered, receipt codes visible
status, html, _ = admin.req("GET", "/")
check("landing remembers code + shows current user", "Ahora estás como" in html and 'id="codigo"' not in html)
check("landing shows receipt codes", "Códigos de recibido para practicar" in html)

# ---------------------------------------------------------------- actions
# administración approves a quotation of the seller
res = admin.rpc("sale.order", "search_read",
                [[["state", "=", "draft"], ["user_id.login", "=", "practica.vendedor"]]],
                {"fields": ["name", "state"], "limit": 1})
orders = res.get("result") or []
check("admin sees a quotation to approve", bool(orders), json.dumps(res.get("error", ""))[:200])
if orders:
    oid, oname = orders[0]["id"], orders[0]["name"]
    res = admin.rpc("sale.order", "action_confirm", [[oid]])
    check("admin approves %s" % oname, "error" not in res, json.dumps(res.get("error", ""))[:300])
    res = admin.rpc("sale.order", "read", [[oid], ["state"]])
    state = (res.get("result") or [{}])[0].get("state")
    check("order %s is confirmed" % oname, state == "sale", str(state))
    sessions["_approved_order"] = oname

# cashier: the box is open; registers an outflow (nafta)
status, html, _ = cajero.req("GET", "/caja")
check("cashier box open", status == 200 and "/caja/salida" in html, str(status))
token = cajero.csrf("/caja/salida")
status, html, path = cajero.post_form("/caja/salida", {"csrf_token": token, "tipo": "nafta",
                                                       "importe": "1500", "quien": "YPF práctica",
                                                       "nota": "Prueba e2e"})
check("cashier outflow saved", status == 200 and "salieron" in html, "%s %s" % (status, path))

# driver: loads his route from the app API
status, data, _ = driver.req("GET", "/reparto/api/route")
try:
    payload = json.loads(data)
except ValueError:
    payload = {}
route = payload.get("route") or {}
check("driver route API", status == 200 and payload.get("ok") and route,
      "%s routes=%s" % (status, [r.get("state") for r in payload.get("routes", [])]))
sessions["_driver_route"] = {"name": route.get("name"), "stops": len(route.get("stops") or route.get("visits") or [])}

# depot: the dispatch API lists today's routes
status, data, _ = depot.req("GET", "/despacho/api/routes")
try:
    routes = json.loads(data).get("routes", [])
except ValueError:
    routes = []
check("depot dispatch API lists routes", status == 200 and len(routes) >= 1, "%s %d" % (status, len(routes)))

# banner on the Odoo backend and the website shop
status, html, _ = admin.req("GET", "/odoo")
check("backend has banner", status == 200 and BANNER in html, str(status))
status, html, _ = Client().req("GET", "/web/login")
check("login page has banner", status == 200 and BANNER in html, str(status))
status, html, _ = Client().req("GET", "/shop")
check("website shop has banner", BANNER in html, str(status))
# database manager closed
# (Caddy also answers 404 on /web/database*; here we hit Odoo directly)
status, h, _ = Client().req("GET", "/web/database/manager", follow=False)
check("database manager disabled (list_db = False)", status >= 400 or "disabled" in h, str(status))
status, h, _ = Client().req("POST", "/web/database/list", body=json.dumps(
    {"jsonrpc": "2.0", "method": "call", "params": {}}), headers={"Content-Type": "application/json"})
check("database list refused", '"error"' in h or status >= 400, str(status))

out = sys.argv[1] if len(sys.argv) > 1 else None
if out:
    with open(out, "w") as fh:
        json.dump({"results": results, "sessions": sessions}, fh, indent=1, ensure_ascii=False)
print("RESUMEN %d ok, %d fallas" % (len(results) - len(failures), len(failures)))
sys.exit(1 if failures else 0)
