"""Fetch every customer page on the one-off server and grep it.

Pass: no invoice number (0001/YYYY/NNNNN) and no word "factura" on any
customer page, the remito appears where there is one, and the balance on
the page is the same one the back office computes.
"""
import html, json, re, sys, urllib.request

BASE = "http://127.0.0.1:8169"
INVOICE = re.compile(r"\b\d{4}/\d{4}/\d{5}\b")
pages = json.load(open("/scripts/out/pages.json"))
flow = json.load(open("/scripts/out/flow.json"))


def get(path):
    with urllib.request.urlopen(BASE + path) as r:
        return r.status, r.read().decode()


def money(v):
    return "{:,.0f}".format(abs(v)).replace(",", ".")


bad = []
rows = 0
for pid, p in pages.items():
    status, text = get("/pedido/%s/cuenta" % p["token"])
    found = INVOICE.findall(text)
    m = re.search(r'<div class="monto">\$ ([0-9.]+)</div>', text)
    shown = m and m.group(1)
    rows += text.count('class="mov"')
    if status != 200 or found or "factura" in text.lower() or shown != money(p["balance"]):
        bad.append((pid, p["name"], status, found[:3], shown, money(p["balance"])))
print("account pages", len(pages), "movement rows shown", rows, "problems", len(bad))
for b in bad[:20]:
    print("  BAD", b)

status, text = get("/pedido/%s" % flow["token"])
checks = {
    "order page 200": status == 200,
    "remito %s shown" % flow["remito"]: flow["remito"] in text,
    "invoice %s hidden" % flow["invoice"]: flow["invoice"] not in text,
    "no invoice-like number": not INVOICE.findall(text),
    "no 'factura'": "factura" not in text.lower(),
}
status, text = get("/pedido/%s/cuenta" % flow["token"])
checks.update({
    "account shows remito": flow["remito"] in text,
    "account hides invoice + credit note": flow["invoice"] not in text and flow["credit_note"] not in text,
    "account shows the return": "Devolución" in text,
    "account shows the cash receipt %s" % flow["payment"]: flow["payment"] in text,
})
for k, v in checks.items():
    print("  %-45s %s" % (k, "OK" if v else "FAIL"))
m = re.search(r'id="como-pagar".*?</div>\s*</div>', get("/pedido/%s" % flow["token"])[1], re.S)
if m:
    print("  Como pagar box:", " ".join(html.unescape(re.sub(r"<[^>]+>", " ", m.group(0))).split())[:200])
sys.exit(1 if bad or not all(checks.values()) else 0)
