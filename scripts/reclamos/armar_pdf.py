import json, re, sys
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether, PageBreak)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

pdfmetrics.registerFont(TTFont('DV', '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'))
pdfmetrics.registerFont(TTFont('DVB', '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'))
fichas = json.load(open(sys.argv[1]))
saldos = json.load(open(sys.argv[2]))
out = sys.argv[3]
EXCLUIR = {'ODOO-143': 'Ivo Laurenti: esperando la respuesta del punto 14 (transferencia del 15/09).',
           'ODOO-519': 'Castillo Jessica: esperando la respuesta del punto 16.',
           'ODOO-110': 'Martino Valeria: esperando la respuesta del punto 20 (deuda de antes de Odoo).',
           'ODOO-151': 'Arnez Albina: debe $316.000, que es justo el efectivo del 06/10 a Jorge que figura en Online; falta verificarlo en la planilla.',
           'ODOO-722': 'Y - Marcos: llegó el comprobante del 17/09 y quedó en $0.',
           'ODOO-69': 'Li Richieri: solo quedan $250 de diferencia.',
           'ODOO-618': 'Rodríguez Yohana: solo quedan $100 de diferencia.'}
INK = colors.HexColor('#1b2230'); MUTED = colors.HexColor('#5f6878'); LINE = colors.HexColor('#dde1e8'); SOFT = colors.HexColor('#f3f5f8')
st = lambda n, **k: ParagraphStyle(n, fontName=k.pop('f', 'DV'), textColor=k.pop('c', INK), **k)
H1 = st('h1', f='DVB', fontSize=17, leading=21, spaceAfter=2)
SUB = st('sub', fontSize=9.5, leading=13, c=MUTED)
H2 = st('h2', f='DVB', fontSize=12.5, leading=16, spaceBefore=10, spaceAfter=4)
H3 = st('h3', f='DVB', fontSize=10.5, leading=13.5)
BODY = st('b', fontSize=9.3, leading=12.6)
MSG = st('m', fontSize=9.3, leading=12.8, leftIndent=8)
SMALL = st('s', fontSize=8.3, leading=11, c=MUTED)

def money(v): return '$' + f'{v:,.0f}'.replace(',', '.')
def ref(t): m = re.search(r'\((ODOO-\d+)\)', t); return m.group(1) if m else None
def vendedor(t):
    m = re.search(r'vendedor(?:es)?:\s*(.+)$', t)
    return m.group(1).strip() if m else 'Sin vendedor asignado'
def nombre(t): return re.sub(r'\s*\(ODOO-\d+\).*$', '', t).strip()

items = []
for f in fichas:
    r = ref(f['titulo'])
    if not r or r in EXCLUIR or not f['mensaje']: continue
    msg = f['mensaje'][-1]
    items.append(dict(ref=r, nombre=nombre(f['titulo']), vend=vendedor(f['titulo']), saldo=saldos[r], msg=msg))
grupos = {}
for it in items:
    clave = it['vend'].split(' y ')[0].split(' e ')[0]
    grupos.setdefault(clave, []).append(it)
orden = sorted(grupos, key=lambda k: (k == 'Sin vendedor asignado', k))

story = [Paragraph('Saldos para reclamar', H1),
         Paragraph('Clientes con la cuenta revisada · 29/10/2026 · saldos del sistema nuevo', SUB), Spacer(1, 8)]
rows = [['Cliente', 'Vendedor', 'Debe']]
for g in orden:
    for it in sorted(grupos[g], key=lambda x: x['nombre']):
        rows.append([Paragraph(it['nombre'], BODY), Paragraph(it['vend'], BODY), money(it['saldo'])])
total = sum(it['saldo'] for it in items)
rows.append([Paragraph('<b>Total (%d clientes)</b>' % len(items), BODY), '', money(total)])
t = Table(rows, colWidths=[95*mm, 45*mm, 30*mm], repeatRows=1)
t.setStyle(TableStyle([('FONT', (0, 0), (-1, 0), 'DVB', 9), ('FONT', (0, 1), (-1, -1), 'DV', 9.3),
    ('TEXTCOLOR', (0, 0), (-1, 0), MUTED), ('LINEBELOW', (0, 0), (-1, 0), 1, INK),
    ('LINEBELOW', (0, 1), (-1, -2), 0.4, LINE), ('LINEABOVE', (0, -1), (-1, -1), 1, INK),
    ('FONT', (2, -1), (2, -1), 'DVB', 9.5), ('ALIGN', (2, 0), (2, -1), 'RIGHT'),
    ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'), ('TOPPADDING', (0, 0), (-1, -1), 4), ('BOTTOMPADDING', (0, 0), (-1, -1), 4)]))
story += [t, Spacer(1, 10), Paragraph('<b>Quedan afuera por ahora</b>', BODY)]
for v in EXCLUIR.values(): story.append(Paragraph('• ' + v, SMALL))
for g in orden:
    story += [PageBreak(), Paragraph('Vendedor: ' + g, H2)]
    for it in sorted(grupos[g], key=lambda x: x['nombre']):
        msg = [Paragraph(l.replace('&', '&amp;'), MSG) for l in it['msg'] if l]
        box = Table([[msg]], colWidths=[170*mm])
        box.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, -1), SOFT), ('LINEBEFORE', (0, 0), (0, -1), 2, INK),
                                 ('TOPPADDING', (0, 0), (-1, -1), 6), ('BOTTOMPADDING', (0, 0), (-1, -1), 6)]))
        story.append(KeepTogether([Paragraph('%s <font color="#5f6878">(%s)</font>' % (it['nombre'], it['ref']), H3),
                                   Paragraph('Debe <b>%s</b> · %s' % (money(it['saldo']), it['vend']), SMALL),
                                   Spacer(1, 3), box, Spacer(1, 9)]))

def pie(c, d):
    c.saveState(); c.setFont('DV', 7.5); c.setFillColor(MUTED)
    c.drawString(18*mm, 10*mm, 'Distrineuquén · Saldos para reclamar · 29/10/2026')
    c.drawRightString(192*mm, 10*mm, 'Página %d' % d.page); c.restoreState()
doc = SimpleDocTemplate(out, pagesize=A4, leftMargin=18*mm, rightMargin=18*mm, topMargin=16*mm, bottomMargin=18*mm,
                        title='Saldos para reclamar', author='Distrineuquén')
doc.build(story, onFirstPage=pie, onLaterPages=pie)
print('OK', len(items), total)
