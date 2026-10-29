# Control 2026-10-28: applies the control's decisions to the COPY
# distri_lab_control (odoo shell body). Never run it on distri_lab.
# Every change uses the lab's own mechanics (action_undo, action_confirm,
# action_online_unlink, _distri_mark_online, _migration_recompute) and leaves
# a note on the card. Prints LOG lines (JSON) and RESULT; rolls back unless
# DO_COMMIT=1 and every assertion holds.
#
# Decisions (see insumos/control-2026-10-28/decisions.csv for the evidence):
#  A. double count: the receipt's movement is also an Online payment -> the
#     lab payment is cancelled and the card linked to Online.
#  B. receipt + movement, not in Online, left pending -> confirmed.
#  C. bookkeeping without balance effect: wrong card customer vs Online,
#     a link to a movement that is not the receipt's, an identity learned
#     from a wrong link.
import json
import os

from datetime import timedelta

from odoo import fields

DO_COMMIT = os.environ.get("DO_COMMIT") == "1"
assert env.cr.dbname == "distri_lab_control", "only the control copy"
uenv = env(user=env.ref("base.user_admin").id)
C = uenv["distri.mp.collection"]
OP = uenv["distri.online.payment"].sudo()
P = uenv["res.partner"].sudo()
AP = uenv["account.payment"].sudo()
logs = []


def log(**kw):
    logs.append(kw)
    print("LOG " + json.dumps(kw, ensure_ascii=False, default=str))


def balance(pid):
    return round(P.browse(pid)._distri_account_balance(), 2)


# ---------------------------------------------------------------- A. doubles
DOUBLES = [
    # card, online name, lab payment, partner, why
    (26432, "PBNK1/2026/03821", "PMP/2026/00526", 201,
     "Guille San Lorenzo $90.000: el comprobante Santander (mensaje 13600, 19/09 18:13) es el pago que "
     "Administración cargó en Online como PBNK1/2026/03821 (lote del 27/09, mismo orden que el grupo: "
     "03819 Albarrán, 03820 José Namuncurá, 03821 Guille, 03822 Luca). Se anula PMP/2026/00526."),
    (27799, "PBNK1/2026/04155", "PMP/2026/00097", 156,
     "Esparza Gastón $277.000 (05/10, mensaje 13855 «AMBAS ESPARZA GASTON»): Online lo cargó el 27/10 "
     "como PBNK1/2026/04155. Se anula PMP/2026/00097."),
    (27798, "PBNK1/2026/04156", "PMP/2026/00098", 156,
     "Esparza Gastón $406.800 (05/10, mensaje 13856 «AMBAS ESPARZA GASTON»): Online lo cargó el 27/10 "
     "como PBNK1/2026/04156. Se anula PMP/2026/00098."),
    (27464, "PBNK1/2026/04157", "PMP/2026/00277", 156,
     "Esparza Gastón $412.000 (15/10, mensaje 14044): Online lo cargó el 27/10 como PBNK1/2026/04157. "
     "Se anula PMP/2026/00277."),
]
for card_id, op_name, pay_name, pid, why in DOUBLES:
    card = C.browse(card_id)
    op = OP.search([("name", "=", op_name)])
    assert len(op) == 1 and op.partner_id.id == pid and abs(op.amount - card.amount) < 1, (card_id, op_name)
    assert card.state == "confirmed" and card.payment_id.name == pay_name and card.partner_id.id == pid, card_id
    before = balance(pid)
    other = op.collection_id
    if other and other != card:
        # the Online payment was tied by amount to a movement without receipt
        assert not other.receipt_id, other.id
        other.action_online_unlink()
        other.sudo().write({"partner_id": False})
        other.sudo().message_post(body="Control 28/10: el pago de Online %s es el del comprobante de %s; "
                                       "este movimiento sin comprobante vuelve al tablero sin cliente." % (op_name, card.name))
        log(action="unlink_online", card=other.id, online=op_name, amount=other.amount)
    card.action_undo()
    assert card.state == "draft" and not card.payment_id
    card.sudo().write({"online_conflict": False})
    op.write({"collection_id": card.id, "link_kind": "amount", "linked_at": fields.Datetime.now()})
    card.sudo()._distri_mark_online(op)
    card.sudo().message_post(body="Control 28/10: %s" % why)
    (card | other).sudo()._migration_recompute()
    env.flush_all()
    after = balance(pid)
    assert abs((after - before) - card.amount) < 0.01, (card_id, before, after)
    log(action="cancel_lab_payment_link_online", card=card.id, payment=pay_name, online=op_name,
        partner=pid, partner_name=card.partner_id.name, amount=card.amount, balance_before=before,
        balance_after=after)

# Mabel Zilleruelo -> Guille: identity learned on 29/09 from the wrong link
ident = uenv["distri.payer.identity"].sudo().search([("kind", "=", "cuit"), ("value", "=", "27122652829")])
if ident:
    assert ident.partner_id.id == 201
    ident.unlink()
    log(action="drop_identity", cuit="27122652829", partner=201,
        why="aprendida el 29/09 del vínculo por importe 26503<->PBNK1/2026/03821, que era de la tarjeta 26432")

# ---------------------------------------------------------------- B. confirm
CONFIRM = [
    # card, partner, receipt message, evidence
    (27651, 193, 13930, "nota de Jorge «Gomez corina», $24.000 08/10, pagador Ernesto Antonio Sánchez (habitual)"),
    (27258, 454, 14163, "nota de Italo «SANDOVAL JOSI», $100.000 21/10, movimiento 17:08 y foto 17:53"),
    (27229, 187, 14182, "nota de Simon «Garrido norma», $100.000 22/10, pagador NORMA DEL CARMEN GARRIDO CANIULLAN"),
    (50343, 503, 14642, "nota de Italo «VAN SCHAIK LUCIANO», $280.800 24/10"),
    (50342, 129, 14643, "nota de Simon «Analía delao», $120.550 24/10, nº de operación coincide"),
    (50341, 920, 14644, "nota de Jorge «Despensa mateo», $140.750 24/10, nº de operación coincide"),
    (50327, 479, 14651, "nota de Gonza «Jessica tamborindegui», $396.200 26/10, nº de operación coincide"),
    (50328, 507, 14653, "nota de Jorge «Valeria tamara amancay vega», $29.900, nº de operación coincide"),
    (50335, 507, 14654, "misma tanda de 3 fotos de Jorge con la nota «Valeria tamara amancay vega», $48.000, pagador tamy, nº de operación coincide"),
    (50324, 507, 14655, "misma tanda de 3 fotos de Jorge, $17.000, pagador tamy, nº de operación coincide"),
    (50333, 431, 14680, "nota de Simon «Mícaela pacífica», $182.720 27/10, nº de operación coincide"),
]
for card_id, pid, msg_id, evidence in CONFIRM:
    card = C.browse(card_id)
    partner = P.browse(pid)
    assert partner.exists() and partner.ref, pid
    assert card.state == "draft" and card.kind == "income" and card.receipt_id.id == msg_id, (card_id, card.state, card.receipt_id.id)
    assert not card.online_payment_id and not card.reversal, card_id
    same = OP.search([("partner_id", "=", partner.id), ("journal_name", "=", "Bank"), ("amount", ">=", card.amount - 1),
                      ("amount", "<=", card.amount + 1), ("date", ">=", card.date_day - timedelta(days=3)),
                      ("date", "<=", card.date_day + timedelta(days=10))])
    assert not same, ("already in Online?", card_id, same.mapped("name"))
    before = balance(partner.id)
    if card.partner_id != partner:
        card.sudo().write({"partner_id": partner.id, "proposal": "manual", "proposal_note": "Control 28/10"})
    card.sudo().message_post(body="Control 28/10: confirmado por la regla (comprobante del grupo + movimiento del "
                                  "reporte MP, no está en Online). %s." % evidence)
    card.with_context(distri_skip_confirm_notice=True).action_confirm()
    env.flush_all()
    after = balance(partner.id)
    assert card.state == "confirmed" and card.payment_id and abs((before - after) - card.amount) < 0.01, (card_id, before, after)
    log(action="confirm", card=card.id, payment=card.payment_id.name, partner=partner.id, partner_name=partner.name,
        amount=card.amount, receipt=msg_id, balance_before=before, balance_after=after)

# ---------------------------------------------------------------- C. bookkeeping
FIX_PARTNER = [
    (26216, 377, "PBNK1/2026/03937", "la nota de Italo dice «PAINEN BELEN» y Online lo cargó a PAINEN BELEN; la tarjeta decía ROCIO PAINEN"),
    (27086, 193, "PBNK1/2026/04054", "la nota de Italo dice «GÓMEZ CORINA» y Online lo cargó a GOMEZ CORINA; la tarjeta decía KATHY - DESPENSA EL COMIENZO"),
    (27841, 312, "PBNK1/2026/04115", "la nota de Gonza dice «Sol agustina migliore» y Online lo cargó a MEGLIORE SOL; la tarjeta decía VEGA FACUNDO (por el CUIT aprendido del caso 868)"),
]
for card_id, pid, op_name, why in FIX_PARTNER:
    card = C.browse(card_id)
    partner = P.browse(pid)
    assert partner.exists() and card.online_payment_id.name == op_name and card.online_payment_id.partner_id == partner, card_id
    assert card.state == "ignored" and not card.payment_id
    old = card.partner_id.name
    card.sudo().write({"partner_id": partner.id})
    card.sudo().message_post(body="Control 28/10: cliente corregido %s → %s. %s." % (old, partner.name, why))
    card.sudo()._migration_recompute()
    log(action="fix_card_partner", card=card.id, old=old, new=partner.name, online=op_name)

# the 29/09 «Eterno Combate» PDF ($247.500) is a bank transfer that Online has
# as PBNK1/2026/03964; the lab tied that Online payment to an MP movement of
# 04/08 without receipt (26107). The movement goes back to the board, the
# Online payment stays (it is in the balance either way).
card = C.browse(26107)
op = card.online_payment_id
assert op.name == "PBNK1/2026/03964" and card.date_day.isoformat() == "2026-08-04" and card.receipt_id.id == 13738
receipt = card.receipt_id.sudo()
card.action_online_unlink()
card.sudo().write({"partner_id": False, "receipt_id": False})
receipt.with_context(distri_receipt_matching=True).write({
    "distri_collection_id": False, "distri_receipt_state": "registered",
    "distri_receipt_note": "Ya cargado en Online: PBNK1/2026/03964 del 29/09/2026 (transferencia bancaria, no es un "
                           "movimiento de Mercado Pago)."})
log(action="detach_receipt", card=26107, receipt=13738, why="el PDF del 29/09 estaba pegado a un movimiento MP del 04/08")
card.sudo().message_post(body="Control 28/10: PBNK1/2026/03964 ($247.500, 29/09, Supermercado Eterno) es el PDF "
                             "«Operacion-0396113772» del grupo (29/09 11:00, nota «Eterno Combate»), una transferencia "
                             "bancaria de ese día; este movimiento de MP del 04/08 no es ese pago y vuelve al tablero.")
card.sudo()._migration_recompute()
log(action="unlink_online", card=26107, online="PBNK1/2026/03964", amount=card.amount, why="movimiento del 04/08 pegado a un pago del 29/09")

env.flush_all()
print("RESULT " + json.dumps({"doubles": len(DOUBLES), "confirmed": len(CONFIRM), "partner_fixes": len(FIX_PARTNER),
                             "logs": len(logs)}))
if DO_COMMIT:
    env.cr.commit()
    print("COMMITTED")
else:
    env.cr.rollback()
    print("NOT COMMITTED")
