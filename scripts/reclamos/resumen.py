import json, sys
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether, PageBreak
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

pdfmetrics.registerFont(TTFont('DV', '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'))
pdfmetrics.registerFont(TTFont('DVB', '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'))
items = json.load(open(sys.argv[1])); sistema = json.load(open(sys.argv[2])); out = sys.argv[3]
NOTAS = {
 'ODOO-47': 'En el sistema figura $741.710: la transferencia de $300.000 del 01/06 está a confirmar (falta el comprobante).',
 'ODOO-427': 'En el sistema figura $564.315: se reclama desde la factura del 24/04, como se acordó. La transferencia de $90.900 del 30/09 está a confirmar.',
}
INK = colors.HexColor('#1b2230'); MUTED = colors.HexColor('#5f6878'); LINE = colors.HexColor('#dde1e8'); SOFT = colors.HexColor('#f3f5f8'); WARN = colors.HexColor('#9a3412')
st = lambda n, **k: ParagraphStyle(n, fontName=k.pop('f', 'DV'), textColor=k.pop('c', INK), **k)
H1 = st('h1', f='DVB', fontSize=17, leading=21, spaceAfter=2); SUB = st('sub', fontSize=9.5, leading=13, c=MUTED)
H3 = st('h3', f='DVB', fontSize=10.5, leading=13.5); BODY = st('b', fontSize=9.3, leading=12.6)
MSG = st('m', fontSize=9.3, leading=12.8, leftIndent=8); SMALL = st('s', fontSize=8.3, leading=11, c=MUTED)
NOTE = st('n', fontSize=8.5, leading=11.5, c=WARN)
money = lambda v: ('-' if v < 0 else '') + '$' + f'{abs(v):,.0f}'.replace(',', '.')
esc = lambda s: s.replace('&', '&amp;').replace('<', '&lt;')
items.sort(key=lambda x: x[1])
story = [Paragraph('Saldos para reclamar · Clientes de Italo', H1),
         Paragraph('Cuentas revisadas · 30/10/2026 · saldos del sistema nuevo. El estado de cuenta de cada cliente va adjunto.', SUB), Spacer(1, 8)]
rows = [['Cliente', 'Vendedor', 'Se reclama', 'Sistema']]
for ref, nom, ven, monto, _ in items:
    s = sistema[ref]['saldo']
    rows.append([Paragraph(esc(nom) + (' *' if ref in NOTAS else ''), BODY), Paragraph(ven, BODY), money(monto), money(s)])
total = sum(i[3] for i in items)
rows.append([Paragraph('<b>Total a reclamar (%d clientes)</b>' % len(items), BODY), '', money(total), ''])
t = Table(rows, colWidths=[78*mm, 32*mm, 30*mm, 30*mm], repeatRows=1)
t.setStyle(TableStyle([('FONT', (0, 0), (-1, 0), 'DVB', 9), ('FONT', (0, 1), (-1, -1), 'DV', 9.3),
    ('TEXTCOLOR', (0, 0), (-1, 0), MUTED), ('TEXTCOLOR', (3, 1), (3, -1), MUTED), ('LINEBELOW', (0, 0), (-1, 0), 1, INK),
    ('LINEBELOW', (0, 1), (-1, -2), 0.4, LINE), ('LINEABOVE', (0, -1), (-1, -1), 1, INK),
    ('FONT', (2, -1), (2, -1), 'DVB', 9.5), ('ALIGN', (2, 0), (3, -1), 'RIGHT'),
    ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'), ('TOPPADDING', (0, 0), (-1, -1), 4), ('BOTTOMPADDING', (0, 0), (-1, -1), 4)]))
story += [t, Spacer(1, 8), Paragraph('<b>* Diferencias con el sistema</b>', BODY)]
for ref, nom, *_ in items:
    if ref in NOTAS: story.append(Paragraph('• <b>%s:</b> %s' % (esc(nom), NOTAS[ref]), SMALL))
story.append(PageBreak())
for ref, nom, ven, monto, msg in items:
    parts = [Paragraph('%s <font color="#5f6878">(%s)</font>' % (esc(nom), ref), H3),
             Paragraph('Se reclama <b>%s</b> · %s' % (money(monto), ven), SMALL)]
    if ref in NOTAS: parts.append(Paragraph(NOTAS[ref], NOTE))
    if msg:
        box = Table([[[Paragraph(esc(l), MSG) for l in msg]]], colWidths=[170*mm])
        box.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, -1), SOFT), ('LINEBEFORE', (0, 0), (0, -1), 2, INK),
                                 ('TOPPADDING', (0, 0), (-1, -1), 6), ('BOTTOMPADDING', (0, 0), (-1, -1), 6)]))
        parts += [Spacer(1, 3), box]
    story.append(KeepTogether(parts + [Spacer(1, 9)]))
def pie(c, d):
    c.saveState(); c.setFont('DV', 7.5); c.setFillColor(MUTED)
    c.drawString(18*mm, 10*mm, 'Distrineuquén · Saldos para reclamar · Clientes de Italo · 30/10/2026')
    c.drawRightString(192*mm, 10*mm, 'Página %d' % d.page); c.restoreState()
SimpleDocTemplate(out, pagesize=A4, leftMargin=18*mm, rightMargin=18*mm, topMargin=16*mm, bottomMargin=18*mm,
                  title='Saldos para reclamar - Italo', author='Distrineuquén').build(story, onFirstPage=pie, onLaterPages=pie)
print('OK', len(items), total)
