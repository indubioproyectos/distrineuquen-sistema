# Practice data of the day for distri_practica, so every role has something
# to do on arrival. Odoo shell script, run by reiniciar.sh right after the
# golden copy is restored (dates are "today", Argentine time):
#   docker compose run --rm -T odoo odoo shell -d distri_practica --no-http < sembrar.py
# Uses only anonymized customers and the real catalog. Nothing is sent: the
# practice DB has no mail server, no crons and no API tokens.
#
# What it leaves:
#   - Repartidor 1: yesterday's route closed on the phone (the cashier receives
#     it), one visit «transfiere más tarde» (a pending receipt), and today's
#     route ON THE STREET with orders and receipt codes (codes on the landing).
#   - Repartidor 2: today's route assigned, not loaded (Depósito controls it).
#   - packed orders without a route (to plan in /rutas), one «lo retira».
#   - approved orders waiting for the packer (/armado).
#   - quotations of the seller waiting for approval (administración).
#   - the practice cashier's box open (/caja).
#   - two transfer receipts (images) waiting for review.
#   - the seller's portfolio (cartera) of practice customers.
#   - «Cliente de práctica» (portal user practica.cliente, the landing's
#     «Hacer un pedido como cliente»): a contact of one anonymized customer
#     with a (fake) phone, a small balance and the account visible to him.
import json
import secrets
import uuid
from datetime import datetime, timedelta

from odoo import Command, fields

from odoo.addons.distri_pedido.models.reparto_cliente import today_ar

assert env.cr.dbname == 'distri_practica', env.cr.dbname
ICP = env['ir.config_parameter'].sudo()
assert ICP.get_param('distri_practica.activo') == '1', 'not a practice database'

TAG = 'Práctica'
R = env.ref
U = {k: R('distri_practica.user_' + k) for k in (
    'armador', 'repartidor_1', 'repartidor_2', 'deposito', 'cajero', 'administracion',
    'gerencia', 'vendedor')}
admin, armador, deposito = U['administracion'], U['armador'], U['deposito']
for user in U.values():
    # unusable passwords, new every night: the landing is the only way in
    user.sudo().write({'password': secrets.token_urlsafe(48), 'active': True})
out = {'orders': {}, 'routes': {}}

# ------------------------------------------------------------ customers and products
Partner = env['res.partner'].sudo()
SO = env['sale.order'].sudo()
Zone = env['distri.zona'].sudo()
customers = Partner.search([
    ('customer_rank', '>', 0), ('active', '=', True), ('is_company', '=', True),
    ('name', '=like', '%Cliente ____%'), ('distri_generic_kind', '=', False),
], order='id')
assert len(customers) >= 30, 'few anonymized customers: %d' % len(customers)


def zone_of(partner):
    probe = SO.new({'partner_id': partner.id})
    return probe._distri_route_zone()


by_zone = {}
for partner in customers:
    zone = zone_of(partner)
    if zone:
        by_zone.setdefault(zone.id, Partner)
        by_zone[zone.id] |= partner
assert by_zone, 'no customer has a delivery zone'
zones_sorted = sorted(by_zone, key=lambda z: -len(by_zone[z]))
main_zone = Zone.browse(zones_sorted[0])
other_zone = Zone.browse(zones_sorted[1]) if len(zones_sorted) > 1 else main_zone
pool_main = list(by_zone[main_zone.id])
pool_other = list(by_zone[other_zone.id])

# ------------------------------------------------------------ the practice customer (portal)
# One anonymized customer with a phone and a small balance owed; the portal
# user's own contact becomes a contact of it, so /pedido, «Mi cuenta» and the
# order page show that customer. Kept out of the pools below.
cliente_user = R('distri_practica.user_cliente').sudo()
rng0 = secrets.SystemRandom()
candidates = [p for p in customers
              if p.phone and not p.distri_lista_oculta and not p.distri_alta_estado]
rng0.shuffle(candidates)
practice_customer, practice_balance = None, None
for partner in candidates[:80]:
    balance = partner._distri_statement()['balance']
    if 2000 <= balance <= 120000:
        practice_customer, practice_balance = partner, balance
        break
if not practice_customer:
    practice_customer = candidates[0] if candidates else customers[0]
    practice_balance = practice_customer._distri_statement()['balance']
    print('WARN practice customer without a small balance: %s' % practice_balance)
for pool in (pool_main, pool_other):
    if practice_customer in pool:
        pool.remove(practice_customer)
practice_customer.write({'distri_cuenta_visible': True})
cliente_user.partner_id.write({
    'name': 'Cliente de práctica', 'parent_id': practice_customer.id, 'type': 'contact',
    'phone': practice_customer.phone or '299 555-0998', 'email': False, 'customer_rank': 1,
})
cliente_user.write({'password': secrets.token_urlsafe(48), 'active': True,
                    'distri_debe_cambiar_clave': False})
special = R('distri_pedido.product_venta_sin_detalle', raise_if_not_found=False)
env.cr.execute("""
    SELECT l.product_id FROM sale_order_line l
    JOIN product_product pp ON pp.id = l.product_id AND pp.active
    JOIN product_template t ON t.id = pp.product_tmpl_id
    WHERE t.sale_ok AND t.active AND t.list_price > 0 AND t.type = 'consu'
    GROUP BY l.product_id ORDER BY count(*) DESC, l.product_id LIMIT 40""")
products = env['product.product'].sudo().browse([r[0] for r in env.cr.fetchall()])
if special:
    products -= special.product_variant_ids if special._name == 'product.template' else special
if len(products) < 12:
    products |= env['product.product'].sudo().search([
        ('sale_ok', '=', True), ('list_price', '>', 0), ('type', '=', 'consu')], limit=40)
products = products[:24]
assert len(products) >= 8, 'few products'
rng = secrets.SystemRandom()


def lines(n):
    picked = rng.sample(list(products), n)
    vals = []
    for product in picked:
        tmpl = product.product_tmpl_id
        unit = (tmpl.uom_ids[:1] if 'uom_ids' in tmpl._fields else tmpl.uom_id) or tmpl.uom_id
        vals.append(Command.create({'product_id': product.id, 'product_uom_qty': rng.choice((1, 2, 3, 6, 12)),
                                    'product_uom_id': unit.id}))
    return vals


def order(key, partner, n=3, seller=None, date=None, **extra):
    vals = {
        'partner_id': partner.id,
        'order_line': lines(n),
        'user_id': (seller or partner.user_id or admin).id,
    }
    if partner.property_product_pricelist:
        vals['pricelist_id'] = partner.property_product_pricelist.id
    if date:
        vals['date_order'] = date
    vals.update(extra)
    o = SO.with_user(seller or admin).create(vals).sudo()
    if seller:
        o.distri_wa_state = 'staff'
    out['orders'][key] = o
    return o


def approve(o):
    o.with_user(admin).action_confirm()


def pack(o, bultos=2):
    p = o.with_user(armador).sudo()
    err = p.distri_tomar()
    assert not err, (o.name, err)
    err = p.distri_terminar(bultos)
    assert not err, (o.name, err)
    o._distri_ensure_receipt_code()


def ev(route, kind, payload):
    return {'uuid': str(uuid.uuid4()), 'type': kind, 'route_id': route.id,
            'captured_at': fields.Datetime.now().strftime('%Y-%m-%dT%H:%M:%S.000Z'),
            'payload': payload}


def sync(user, events):
    res = env['distri.sync.event'].sudo().process_batch(user, events)
    bad = [r for r in res if r.get('status') != 'ok']
    assert not bad, bad
    return res


def visit(stop, **kw):
    payload = {'visit_uuid': str(uuid.uuid4()), 'stop_id': stop.id, 'outcome': 'delivered',
               'returns_answer': 'none', 'returns': [], 'code': '', 'without_code': True,
               'cash': 0, 'transfer': 'no', 'sent_to_vendor': None, 'no_payment': False,
               'comment': ''}
    payload.update(kw)
    return payload


today = today_ar()
yesterday = today - timedelta(days=1)
yday_morning = datetime.combine(yesterday, datetime.min.time()) + timedelta(hours=11)  # 08:00 AR
Route = env['distri.route'].sudo()
d1, d2 = U['repartidor_1'], U['repartidor_2']

# ------------------------------------------------------------ yesterday: route closed, to receive
y_orders = [order('ayer_%d' % i, pool_main.pop(0), n=3, date=yday_morning) for i in range(3)]
for o in y_orders:
    approve(o)
    pack(o)
r_y = Route.create({'driver_id': d1.id, 'date': yesterday, 'trip': '1',
                    'zone_ids': [Command.set(main_zone.ids)]})
r_y._add_orders(SO.browse([o.id for o in y_orders]))
sync(d1, [ev(r_y, 'load', {'stop_id': s.id, 'loaded': True}) for s in r_y.stop_ids])
r_y.stop_ids.with_user(deposito).action_dispatch_check()
sync(d1, [ev(r_y, 'depart', {})])
s_a, s_b, s_c = r_y.stop_ids.sorted('sequence')
cash_b = round(s_b.order_id.amount_total, -1)
sync(d1, [ev(r_y, 'visit', visit(s_a, transfer='yes', transfer_amount=s_a.order_id.amount_total, receipt_answer='not_yet', comment='Transfirió, manda el comprobante después')),
          ev(r_y, 'visit', visit(s_b, cash=cash_b, comment='Pagó en efectivo')),
          ev(r_y, 'visit', visit(s_c, no_payment=True, comment='Paga la semana que viene')),
          ev(r_y, 'close', {})])
out['routes']['ayer'] = r_y

# ------------------------------------------------------------ today: route 1 on the street
t1 = [order('ruta1_%d' % i, pool_main.pop(0), n=rng.choice((3, 4))) for i in range(4)]
for o in t1:
    approve(o)
    pack(o, bultos=rng.choice((1, 2, 3)))
r1 = Route.with_user(admin).create({'driver_id': d1.id, 'date': today, 'trip': '1',
                                    'zone_ids': [Command.set(main_zone.ids)]})
r1._add_orders(SO.browse([o.id for o in t1]))
sync(d1, [ev(r1, 'load', {'stop_id': s.id, 'loaded': True}) for s in r1.stop_ids])
r1.stop_ids.with_user(deposito).action_dispatch_check()
sync(d1, [ev(r1, 'depart', {})])
out['routes']['hoy_1'] = r1

# ------------------------------------------------------------ today: route 2 assigned, not loaded
t2 = [order('ruta2_%d' % i, pool_other.pop(0), n=3) for i in range(3)]
for o in t2:
    approve(o)
    pack(o)
r2 = Route.with_user(admin).create({'driver_id': d2.id, 'date': today, 'trip': '1',
                                    'zone_ids': [Command.set(other_zone.ids)]})
r2._add_orders(SO.browse([o.id for o in t2]))
out['routes']['hoy_2'] = r2

# ------------------------------------------------------------ packed, without a route
for i in range(3):
    o = order('armado_sin_ruta_%d' % i, pool_main.pop(0), n=3)
    approve(o)
    pack(o)
o = order('retira', pool_main.pop(0), n=2, distri_entrega='retira')
approve(o)
pack(o, bultos=1)

# ------------------------------------------------------------ approved, waiting for the packer
for i in range(5):
    o = order('para_armar_%d' % i, (pool_main if i % 2 else pool_other).pop(0), n=rng.choice((3, 4, 5)))
    approve(o)

# ------------------------------------------------------------ the seller: portfolio + quotations
seller = U['vendedor']
cartera = env['res.partner.category'].sudo().search([('name', '=', 'Cartera de práctica')], limit=1) \
    or env['res.partner.category'].sudo().create({'name': 'Cartera de práctica'})
mine = Partner.browse([p.id for p in (pool_main[:10] + pool_other[:5])])
mine.write({'category_id': [Command.link(cartera.id)], 'user_id': seller.id})
seller.sudo().etiqueta_cartera_id = cartera
for i, partner in enumerate(mine[:2]):
    order('para_aprobar_%d' % i, partner, n=3, seller=seller)

# ------------------------------------------------------------ the cashier's box
caja = env['distri.caja'].with_user(U['cajero']).open_session('50000', note='Caja de práctica')
out['caja'] = caja

# ------------------------------------------------------------ transfer receipts to review
receipts = []
try:
  with env.cr.savepoint():
      from odoo.addons.distri_pedido.tests import receipt_images as ri
      Msg = env['distri.whatsapp.message'].sudo()
      now = fields.Datetime.now()
      for i, partner in enumerate(mine[2:4], start=1):
          amount = float(round(rng.uniform(8000, 60000), -2))
          data = {
              'amount': amount, 'operation': '99%010d' % rng.randrange(10 ** 10),
              'date': now - timedelta(hours=3 + i),
              'payer_name': partner.name.upper(), 'payer_cuit': (partner.vat or '20900000001').replace('-', ''),
              'payer_cvu': '%022d' % (10 ** 21 + partner.id),
              'dest_name': 'Distri Neuquén Práctica', 'dest_cuit': ri.cuit_with_check('30', 1),
              'dest_cvu': '0000003100000000000000', 'dest_alias': 'PRACTICA.DISTRI.NQN',
          }
          png, _extra = ri.mercado_pago(data)
          wa = '549299555%04d' % (9000 + i)
          msg = Msg.create({
              'wa_message_id': 'wamid.PRACTICA-%s-%d' % (today.isoformat(), i), 'direction': 'in',
              'wa_id': wa, 'profile_name': partner.name, 'message_type': 'image',
              'body': 'Te paso el comprobante', 'timestamp': now - timedelta(hours=i),
              'media_mime_type': 'image/png',
          })
          att = env['ir.attachment'].sudo().create({
              'name': 'comprobante-practica-%d.png' % i, 'raw': png, 'mimetype': 'image/png',
              'res_model': msg._name, 'res_id': msg.id})
          msg.write({'attachment_id': att.id, 'media_state': 'done'})
          receipts.append(msg.id)
except Exception as exc:  # receipts are a nice-to-have; the rest of the day must stay
    print('WARN receipts not created: %r' % (exc,))

env.flush_all()
summary = {
    'today': str(today),
    'orders': {k: [o.name, o.state, o.distri_armado] for k, o in out['orders'].items()},
    'routes': {k: [r.name, r.state, len(r.stop_ids)] for k, r in out['routes'].items()},
    'caja': [caja.id, caja.state],
    'receipts': receipts,
    'cliente': [cliente_user.login, practice_customer.name, round(practice_balance or 0, 2)],
    'codes_on_route_1': len([s for s in r1.stop_ids if s.order_id.distri_receipt_code]),
    'promises_pending': env['distri.promised.transfer'].sudo().search_count([('state', '=', 'pending')])
    if 'state' in env['distri.promised.transfer']._fields else None,
}
print('SEMBRADO ' + json.dumps(summary, ensure_ascii=False, default=str))
env.cr.commit()
print('COMMITTED')
