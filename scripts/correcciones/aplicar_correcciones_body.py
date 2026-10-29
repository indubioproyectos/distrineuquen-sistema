# Applies the verified correction plan of the WhatsApp receipts to a lab
# database (odoo shell body, run by scripts/correcciones/aplicar-correcciones.sh).
#
# Step 1 (PASOS=1): plan_fase1.json, verified on 23/10/2026
# (cobranza/comprobantes/verificacion_plan_2026-10-23.md), with the three
# adjustments of that check:
#   - field corrections of the reading (amount, operation, date);
#   - msg 1440 is a copy of 963 (unlinked from MP 176109229605 / Online
#     PBNK1/2026/03430) and msg 1288 a copy of 1284: «Ya recibido»;
#   - the 4 copies of the plan: «Ya recibido»;
#   - the 3 wrong links (relink_wrong), before any firm link, so 1384 finds
#     MP 181220683308 free once 1378 leaves it;
#   - the 165 firm links: «Ya registrado» when the MP income is already in
#     Online (customer taken from the Online payment), else linked to the
#     pending income as the module does.
# Step 2 (PASOS=2): the August report, rebuilt from the ORIGINAL file from
# 28/08 00:00 (approval date) and loaded with the module's importer; then
# the receipts sent 28-31/08 are matched with firm criteria only (operation
# number, operation one digit off with the same amount, amount and time
# within 2 minutes) and close as «Ya registrado» when the income is in Online.
#
# Every action is checked again against the live database: receipt in the
# expected state, MP income loaded and free, Online payment mirrored with the
# same amount and not claimed by another receipt. Otherwise it is skipped and
# listed. Never creates a payment, never confirms, never exports: the run
# stops if the number of payments, confirmed incomes or export batches moves.
# loose_review and untouched of the plan are not applied. Never touched:
# 1352, 1273, 865, 868; 1407 stays for review.
# Rolls back unless DO_COMMIT=1.
import io
import json
import os
from datetime import datetime, timedelta, timezone

from odoo import fields
from odoo.addons.distri_pedido.models.cobros import (
    AMOUNT_TOLERANCE, RECEIPT_TIME_CLOSE, digits, local_when, operation_close)
from odoo.addons.distri_pedido.models.puesta_al_dia import ONLINE_POSTED

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
STEPS = {s.strip() for s in (os.environ.get('PASOS') or '1').split(',') if s.strip()}
Message = env['distri.whatsapp.message'].sudo()
Collection = env['distri.mp.collection'].sudo()
Partner = env['res.partner'].sudo()
MATCHING = {'distri_receipt_matching': True}
OPEN = ('pending', 'no_money')
GONE = ('dismissed', 'duplicate')
RUN_NOTE = 'corrección verificada del 23/10/2026'

# verificacion_plan_2026-10-23.md: never touched in this run
NEVER = {1352: 'transfer to a third party: management decides',
         1273: 'Barros: outside the plan',
         865: 'MP 152.100 vs Online 151.200: review the Online amount',
         868: 'weak match (amount + posting time only)'}
# left for review: no state change (its field correction still applies)
REVIEW = {1407: 'Online customer differs (Paco Colque -> QUISPE - VALENTINA NORTE RURAL)'}
# (copy, original, MP operation both claim): blocking points 1 and 2
EXTRA_DUPLICATES = [(1440, 963, '176109229605'), (1288, 1284, '179183107973')]
AUGUST_FROM = datetime(2026, 8, 28)          # approval date, Argentine time
AUGUST_RECEIPTS = (datetime(2026, 8, 28, 3), datetime(2026, 9, 1, 3))   # 28-31/08 local, as UTC
AR = timezone(timedelta(hours=-3))


def guard_counts():
    return {
        'payments': env['account.payment'].sudo().search_count([]),
        'confirmed_incomes': Collection.search_count([('state', '=', 'confirmed')]),
        'export_batches': env['distri.online.export'].sudo().search_count([]),
    }


def receipt_states():
    env.flush_all()
    env.cr.execute("""select coalesce(distri_receipt_state, 'none'), count(*)
                        from distri_whatsapp_message where distri_is_receipt group by 1""")
    return dict(sorted(env.cr.fetchall()))


def board_counts():
    env.flush_all()
    env.cr.execute("""select kind || ':' || state, count(*) from distri_mp_collection
                       group by 1 order by 1""")
    return dict(env.cr.fetchall())


class Log:
    def __init__(self):
        self.data = {}

    def ok(self, action, rid, **extra):
        entry = self.data.setdefault(action, {'applied': 0, 'applied_ids': [], 'skipped': []})
        entry['applied'] += 1
        entry['applied_ids'].append(dict(id=rid, **extra) if extra else rid)

    def skip(self, action, rid, reason):
        entry = self.data.setdefault(action, {'applied': 0, 'applied_ids': [], 'skipped': []})
        entry['skipped'].append({'id': rid, 'reason': reason})


log = Log()


def get_receipt(rid):
    msg = Message.browse(rid).exists()
    return msg if msg and msg.distri_is_receipt else None


def income_by_op(op):
    op = digits(op)
    return Collection.search([('kind', '=', 'income'), ('source_id', '=', op)], limit=1) if op \
        else Collection.browse()


def pay_label(pay):
    return pay.name or pay.move_name or ''


def claimers(income, pay, exclude):
    """Other live receipts that already claim this MP income or its Online
    payment («Ya cargado en Online: <payment> del …»)."""
    domain = [('distri_is_receipt', '=', True), ('distri_receipt_state', 'not in', GONE),
              ('id', 'not in', list(exclude))]
    terms = [('distri_collection_id', '=', income.id)]
    if pay and pay_label(pay):
        terms.append(('distri_receipt_note', 'ilike', '%s del' % pay_label(pay)))
    found = Message.search(domain + ['|'] * (len(terms) - 1) + terms)
    owner = income.receipt_id
    if owner and owner.id not in exclude and owner.distri_receipt_state not in GONE:
        found |= owner
    return found


def online_problem(income, expected_name=None):
    """Why this income cannot close a receipt as «Ya registrado», or ''."""
    pay = income.online_payment_id
    if not pay:
        return 'mp_not_in_online'
    if expected_name and pay_label(pay) != expected_name:
        return 'online_payment_differs:%s' % pay_label(pay)
    if abs((pay.amount or 0.0) - income.amount) > AMOUNT_TOLERANCE:
        return 'online_amount_differs:%s/%s' % (pay.amount, income.amount)
    if pay.state not in ONLINE_POSTED:
        return 'online_not_posted:%s' % pay.state
    if income.state == 'confirmed' or income.payment_id:
        return 'mp_confirmed_in_lab'
    return ''


def amount_problem(msg, income):
    if msg.distri_amount and abs(msg.distri_amount - income.amount) > AMOUNT_TOLERANCE:
        return 'amount_differs:%s/%s' % (msg.distri_amount, income.amount)
    return ''


def register(msg, income, how):
    """«Ya registrado»: the module's own link (`_link_receipt` closes the
    «Foto sin transferencia» / «Esperando reporte MP» card as «La plata
    llegó»), then the close `_distri_mark_online` writes. The customer comes
    from the Online payment."""
    pay = income.online_payment_id
    income._link_receipt(msg)
    partner = pay.partner_id.commercial_partner_id
    lab = msg.distri_partner_id.commercial_partner_id
    changed = ('replaced' if lab else 'filled') if partner and lab != partner else False
    if changed:
        msg.with_context(**MATCHING).write({
            'distri_partner_id': partner.id,
            'distri_partner_source': 'Cliente del pago de Online %s' % pay_label(pay)})
    msg._distri_close('registered', 'Ya cargado en Online: %s del %s.' % (
        pay_label(pay), pay.date.strftime('%d/%m/%Y') if pay.date else ''))
    income.message_post(body='Comprobante %s unido (%s, %s): ya cargado en Online como %s.' % (
        msg.display_name, how, RUN_NOTE, pay_label(pay)))
    return changed


def link_pending(msg, income, how):
    """The receipt of a pending income, as `_distri_match_income` does."""
    income._link_receipt(msg)
    income._propose()
    income.message_post(body='Comprobante %s unido (%s, %s).' % (msg.display_name, how, RUN_NOTE))


def free_income(income, msg):
    """A wrong link undone without the by-amount pairing of `_propose`: the
    income keeps only what its own data says (a known CUIT)."""
    vals = {'receipt_id': False, 'conflict_note': False}
    if income.state == 'draft' and income.proposal in ('photo', 'operation', 'none'):
        partners = Partner._distri_partners_by_cuit(income.payer_cuit) if income.payer_cuit \
            else Partner.browse()
        if len(partners) == 1:
            vals.update(partner_id=partners.id, proposal='cuit', proposal_note='CUIT conocido')
        else:
            vals.update(partner_id=False, proposal='none', proposal_note=False)
    income.write(vals)
    income.message_post(body='Comprobante %s quitado: era de otra transferencia (%s).' % (
        msg.display_name, RUN_NOTE))


def close_duplicate(msg, original):
    msg._distri_close('duplicate', 'Ya recibido el %s (%s).' % (
        local_when(original.timestamp),
        original.distri_forwarded_by_id.name or original.profile_name or original.wa_id or ''))
    # `_distri_sync_verify_card` closes «Esperando reporte MP»; the «Foto sin
    # transferencia» card closes with the same reason
    Collection.search([('kind', '=', 'receipt'), ('receipt_id', '=', msg.id),
                       ('state', '=', 'draft')]).write({
        'state': 'ignored', 'ignore_reason': 'receipt_error',
        'ignore_note': msg.distri_receipt_note or False})


# ====================================================================== step 1


def step1(plan):
    # -- 1. field corrections (no matching runs: distri_receipt_matching)
    for item in plan['field_updates']:
        rid = item['id']
        if rid in NEVER:
            log.skip('field_updates', rid, 'excluded: ' + NEVER[rid])
            continue
        msg = get_receipt(rid)
        if not msg:
            log.skip('field_updates', rid, 'receipt_not_found')
            continue
        vals = {}
        for key, value in item['vals'].items():
            current = msg[key]
            if key == 'distri_receipt_date':
                value = fields.Datetime.to_datetime(value)
            elif key == 'distri_amount':
                value = float(value)
            if current != value:
                vals[key] = value
        if not vals:
            log.skip('field_updates', rid, 'already_applied')
            continue
        if 'distri_amount' in vals:
            # checked by a person: nothing read later replaces it
            vals['distri_amount_confidence'] = 'manual'
        msg.with_context(**MATCHING).write(vals)
        msg._distri_sync_verify_card()
        Collection.search([('kind', '=', 'receipt'), ('receipt_id', '=', msg.id),
                           ('state', '=', 'draft')]).filtered(
            lambda c: c.amount != msg.distri_amount).write({'amount': msg.distri_amount})
        log.ok('field_updates', rid)

    # -- 2. copies found by the check: 1440 (of 963), 1288 (of 1284)
    for rid, of, op in EXTRA_DUPLICATES:
        msg, original, income = get_receipt(rid), get_receipt(of), income_by_op(op)
        if not (msg and original):
            log.skip('extra_duplicates', rid, 'receipt_not_found')
            continue
        if msg.distri_receipt_state != 'registered':
            log.skip('extra_duplicates', rid, 'state:%s' % msg.distri_receipt_state)
            continue
        if original.distri_receipt_state in GONE:
            log.skip('extra_duplicates', rid, 'original_state:%s' % original.distri_receipt_state)
            continue
        moved_to = None
        if income and income.receipt_id == msg:
            # the original keeps the claim only when it already had it
            pay = income.online_payment_id
            if original.distri_receipt_state == 'registered' and pay and \
                    pay_label(pay) in (original.distri_receipt_note or '') and \
                    not original.distri_collection_id:
                income.receipt_id = original
                original.with_context(**MATCHING).write({'distri_collection_id': income.id})
                moved_to = original.id
            else:
                income.receipt_id = False
            income.message_post(body='Comprobante %s quitado: es copia de %s (%s).' % (
                msg.display_name, original.display_name, RUN_NOTE))
        if income and msg.distri_collection_id == income:
            msg.with_context(**MATCHING).write({'distri_collection_id': False})
        close_duplicate(msg, original)
        log.ok('extra_duplicates', rid, of=of, mp_receipt_moved_to=moved_to)

    # -- 3. copies of the plan
    for item in plan['duplicates']:
        rid, of = item['id'], item['of']
        msg, original = get_receipt(rid), get_receipt(of)
        if rid in NEVER or rid in REVIEW:
            log.skip('duplicates', rid, 'excluded')
            continue
        if not (msg and original):
            log.skip('duplicates', rid, 'receipt_not_found')
            continue
        if msg.distri_receipt_state not in OPEN:
            log.skip('duplicates', rid, 'state:%s' % msg.distri_receipt_state)
            continue
        if original.distri_receipt_state in GONE:
            log.skip('duplicates', rid, 'original_state:%s' % original.distri_receipt_state)
            continue
        close_duplicate(msg, original)
        log.ok('duplicates', rid, of=of)

    # -- 4. wrong links, before the firm ones (1378 frees MP 181220683308 for 1384)
    for item in plan['relink_wrong']:
        rid = item['id']
        msg = get_receipt(rid)
        wrong, real = income_by_op(item['wrong_mp']), income_by_op(item['real_mp'])
        if rid in NEVER or rid in REVIEW:
            log.skip('relink_wrong', rid, 'excluded')
            continue
        if not msg:
            log.skip('relink_wrong', rid, 'receipt_not_found')
            continue
        if msg.distri_receipt_state != 'matched' or msg.distri_collection_id != wrong:
            log.skip('relink_wrong', rid, 'state:%s/%s' % (
                msg.distri_receipt_state, msg.distri_collection_id.name or ''))
            continue
        if not wrong or wrong.state != 'draft' or wrong.receipt_id != msg:
            log.skip('relink_wrong', rid, 'wrong_mp_state:%s' % (wrong.state if wrong else 'missing'))
            continue
        if not real:
            log.skip('relink_wrong', rid, 'real_mp_missing')
            continue
        problem = online_problem(real, item['online_payment']) or amount_problem(msg, real)
        others = claimers(real, real.online_payment_id, {msg.id})
        if not problem and others:
            problem = 'claimed_by:%s' % ','.join(str(i) for i in others.ids)
        if problem:
            log.skip('relink_wrong', rid, problem)
            continue
        free_income(wrong, msg)
        register(msg, real, 'reunión verificada')
        log.ok('relink_wrong', rid, mp=item['real_mp'], online=item['online_payment'])

    # -- 5. firm links
    for item in plan['firm']:
        rid = item['id']
        action = 'firm_registered' if item['online'] else 'firm_pending_income'
        if rid in NEVER:
            log.skip(action, rid, 'excluded: ' + NEVER[rid])
            continue
        if rid in REVIEW:
            log.skip(action, rid, 'review: ' + REVIEW[rid])
            continue
        msg, income = get_receipt(rid), income_by_op(item['mp'])
        if not msg:
            log.skip(action, rid, 'receipt_not_found')
            continue
        if msg.distri_receipt_state not in OPEN:
            log.skip(action, rid, 'state:%s' % msg.distri_receipt_state)
            continue
        if not income:
            log.skip(action, rid, 'mp_missing:%s' % item['mp'])
            continue
        problem = amount_problem(msg, income)
        live_online = bool(income.online_payment_id)
        if not problem and item['online'] and not live_online:
            problem = 'not_in_online_now'
        if not problem and not item['online'] and live_online:
            problem = 'now_in_online:%s' % pay_label(income.online_payment_id)
        if not problem and item['online']:
            problem = online_problem(income)
        if not problem and not item['online'] and income.state != 'draft':
            problem = 'mp_state:%s' % income.state
        if not problem:
            others = claimers(income, income.online_payment_id, {msg.id})
            if others:
                problem = 'claimed_by:%s' % ','.join(str(i) for i in others.ids)
        if problem:
            log.skip(action, rid, problem)
            continue
        if item['online']:
            changed = register(msg, income, 'firme: %s' % item['how'])
            log.ok(action, rid, mp=item['mp'], online=pay_label(income.online_payment_id),
                   partner_from_online=changed)
        else:
            link_pending(msg, income, 'firme: %s' % item['how'])
            log.ok(action, rid, mp=item['mp'])


# ====================================================================== step 2


def august_report(path):
    """The ORIGINAL August report from 28/08 00:00 (approval date, Argentine
    time) as .xlsx bytes, same columns and cell values."""
    import openpyxl
    book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    rows = list(book.worksheets[0].iter_rows(values_only=True))
    book.close()
    header, body = rows[0], rows[1:]
    col = header.index('FECHA DE APROBACIÓN')

    def approved(value):
        if isinstance(value, datetime):
            moment = value
        else:
            try:
                moment = datetime.fromisoformat(str(value or '').strip().replace('Z', '+00:00'))
            except ValueError:
                return None
        if moment.tzinfo:
            moment = moment.astimezone(AR).replace(tzinfo=None)
        return moment

    kept = [r for r in body if (approved(r[col]) or datetime.min) >= AUGUST_FROM]
    out = openpyxl.Workbook()
    sheet = out.active
    sheet.append(list(header))
    for row in kept:
        sheet.append(list(row))
    data = io.BytesIO()
    out.save(data)
    return data.getvalue(), len(body), len(kept)


def firm_income(msg):
    """(income, how) by firm criteria only, or (None, reason)."""
    op = digits(msg.distri_operation)
    amount = msg.distri_amount
    base = [('kind', '=', 'income')]
    if op:
        found = Collection.search(base + ['|', ('source_id', '=', op), ('bank_transfer_id', '=', op)])
        if len(found) == 1:
            return found, 'op'
        if len(found) > 1:
            return None, 'op_ambiguous'
        if amount:
            near = Collection.search(base + [('amount', '>=', amount - AMOUNT_TOLERANCE),
                                             ('amount', '<=', amount + AMOUNT_TOLERANCE)]).filtered(
                lambda c: operation_close(op, c.source_id) or operation_close(op, c.bank_transfer_id))
            if len(near) == 1:
                return near, 'op_1digit'
    if amount and msg.distri_receipt_date:
        read = msg.distri_receipt_date
        near = Collection.search(base + [('amount', '>=', amount - AMOUNT_TOLERANCE),
                                         ('amount', '<=', amount + AMOUNT_TOLERANCE),
                                         ('date', '>=', read - RECEIPT_TIME_CLOSE),
                                         ('date', '<=', read + RECEIPT_TIME_CLOSE)])
        if len(near) == 1:
            return near, 'amount_time'
        if len(near) > 1:
            return None, 'amount_time_ambiguous'
    return None, 'no_firm_match'


def step2(path):
    data, total, kept = august_report(path)
    name = os.path.basename(path).replace('.xlsx', '-desde-28-08.xlsx')
    out = {'file': name, 'original_rows': total, 'rows_from_28_08': kept}
    Report = env['distri.mp.report'].sudo()
    if Report.search_count([('name', '=', name)]):
        out['import'] = 'already_imported'
    else:
        # the importer's own receipt matching pairs by amount: every open
        # receipt waits aside while it runs and comes back unchanged
        parked = {}
        for msg in Message.search([('distri_is_receipt', '=', True),
                                   ('distri_receipt_state', 'in', OPEN)]):
            parked.setdefault(msg.distri_receipt_state, []).append(msg.id)
        for ids in parked.values():
            Message.browse(ids).with_context(**MATCHING).write({'distri_receipt_state': 'dismissed'})
        report = Report._import_file(name, data, source='manual').sudo()
        for state, ids in parked.items():
            Message.browse(ids).with_context(**MATCHING).write({'distri_receipt_state': state})
        out.update(report_id=report.id, new=report.new_count, already_loaded=report.duplicate_count,
                   other_rows=report.skipped_count, already_in_online=report.online_count,
                   covers=[str(report.period_start), str(report.covers_until)],
                   new_pending_on_board=len(report.collection_ids.filtered(
                       lambda c: c.kind == 'income' and c.state == 'draft')),
                   flagged_hand_verified=Collection.search_count([('report_id', '=', report.id),
                                                                  ('kind', '=', 'verify')]),
                   parked=sum(len(v) for v in parked.values()))
    receipts = Message.search([('distri_is_receipt', '=', True),
                               ('timestamp', '>=', AUGUST_RECEIPTS[0]),
                               ('timestamp', '<', AUGUST_RECEIPTS[1])], order='timestamp, id')
    out['receipts_28_31'] = len(receipts)
    for msg in receipts:
        rid = msg.id
        if rid in NEVER:
            log.skip('august_registered', rid, 'excluded: ' + NEVER[rid])
            continue
        if msg.distri_receipt_state not in OPEN:
            log.skip('august_registered', rid, 'state:%s' % msg.distri_receipt_state)
            continue
        income, how = firm_income(msg)
        if not income:
            log.skip('august_registered', rid, how)
            continue
        problem = (amount_problem(msg, income) if how != 'op' or msg.distri_amount else '') \
            or online_problem(income)
        if not problem:
            others = claimers(income, income.online_payment_id, {msg.id})
            if others:
                problem = 'claimed_by:%s' % ','.join(str(i) for i in others.ids)
        if problem:
            log.skip('august_registered', rid, '%s (%s %s)' % (problem, how, income.source_id))
            continue
        changed = register(msg, income, 'agosto, firme: %s' % how)
        log.ok('august_registered', rid, mp=income.source_id, how=how,
               online=pay_label(income.online_payment_id), partner_from_online=changed)
    return out


# ====================================================================== run

guard_before = guard_counts()
result = {'steps': sorted(STEPS), 'receipts_before': receipt_states(), 'board_before': board_counts()}
if '1' in STEPS:
    step1(json.load(open(os.environ['PLAN_PATH'], encoding='utf-8')))
if '2' in STEPS:
    result['august_report'] = step2(os.environ['MP_AGOSTO_PATH'])
env.flush_all()
guard_after = guard_counts()
result.update(receipts_after=receipt_states(), board_after=board_counts(),
              guard_before=guard_before, guard_after=guard_after,
              actions={k: {'applied': v['applied'], 'skipped': len(v['skipped'])}
                       for k, v in log.data.items()},
              skipped={k: v['skipped'] for k, v in log.data.items() if v['skipped']},
              applied_ids={k: v['applied_ids'] for k, v in log.data.items()})
safe = guard_before == guard_after
print("RESULT", json.dumps(result, ensure_ascii=False, default=str))
if DO_COMMIT and safe:
    env.cr.commit()
    print("COMMITTED")
else:
    env.cr.rollback()
    print("NOT COMMITTED" + (" (payments, confirmations or exports moved)" if not safe
                             else " (DO_COMMIT is not 1)"))
