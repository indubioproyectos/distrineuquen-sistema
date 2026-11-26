"""19.0.1.129.48 (owner, 26/11/2026): «Mis comprobantes a reclamar» is the
main task of the day, so it is its own block at the top of each one's
screen, not a banner line among others.

This file gathers the items of the Inicio block (controllers/tablero.py
`reclamar_block`, views/tablero.xml): one merged list, without duplicates,
of the receipts the user is asked to help claim:
- «deposito»: the receipts «Lo manda después» left at his name at the depot
  (comprobantes_mostrador.py `_distri_user_items`, still open);
- «reparto»: his own pending receipts as a driver (the same ones the driver
  app lists, `_distri_driver_payload`, still open);
- «vendedor»: the pending receipts of his customers (his portfolio,
  `res.partner._distri_cartera_domain`, or orders he sold), searched in his
  own environment first so the record rules of «Comprobantes pendientes»
  apply; what is only shown (route, order, customer) is read with sudo
  after that filter, since sellers cannot read distri.route.
Which roles apply is decided by the controller (flags, groups, menus).

Read-only: nothing here writes a promise, a payment or an entry. The items
leave the list by themselves when the receipt or the money arrives (the
existing rules of comprobante_pendiente.py). The wording is a request to
collaborate: no totals and nothing «a tu cargo».
"""
from urllib.parse import quote

from odoo import api, models

from .comprobante_pendiente import OPEN_STATES, document_of
from .reparto_cliente import today_ar

ROLE_ORDER = ("deposito", "reparto", "vendedor")


def days_ago(day, today=None):
    """«hoy», «ayer», «hace N días» (the driver app's daysAgo)."""
    if not day:
        return ""
    days = ((today or today_ar()) - day).days
    if days <= 0:
        return "hoy"
    if days == 1:
        return "ayer"
    return "hace %s días" % days


class DistriPromisedTransferInicio(models.Model):
    _inherit = "distri.promised.transfer"

    @api.model
    def _distri_seller_claim_items(self, user):
        """The open receipts of the seller's customers: first what his own
        record rules let him see, then only his portfolio or his orders
        (an administración user sees every receipt, but his block is only
        about his own customers)."""
        visible = self.with_user(user).search([("state", "in", OPEN_STATES)],
                                              order="captured_at, id")
        if not visible:
            return self.sudo().browse()
        Partner = self.env["res.partner"].sudo().with_context(active_test=False)
        mine = set(Partner.search(Partner._distri_cartera_domain(user)).ids)
        shown = visible.sudo()
        return shown.filtered(lambda p: p.order_id.user_id == user
                              or p.partner_id.id in mine
                              or p.partner_id.commercial_partner_id.id in mine)

    @api.model
    def _distri_inicio_claims(self, user, roles):
        """[row] of the Inicio block for `user` and the given roles (a subset
        of ROLE_ORDER), oldest first; one row per receipt even when it
        belongs to several roles (the first role in ROLE_ORDER wins, since
        it has the most direct action)."""
        Promise = self.sudo()
        found = []  # [(role, promise)]
        if "deposito" in roles:
            found += [("deposito", p) for p in Promise._distri_user_items(user)
                      if p.state in OPEN_STATES]
        if "reparto" in roles:
            found += [("reparto", p) for p in Promise.search([
                ("driver_id", "=", user.id), ("route_kind", "=", "route"),
                ("state", "in", OPEN_STATES)], order="captured_at, id")]
        if "vendedor" in roles:
            found += [("vendedor", p) for p in Promise._distri_seller_claim_items(user)]
        seen, picked = set(), []
        for role, promise in found:
            if promise.id in seen:
                continue
            seen.add(promise.id)
            picked.append((role, promise))
        picked.sort(key=lambda rp: (rp[1].captured_at or rp[1].create_date, rp[1].id))
        today = today_ar()
        return [self._distri_inicio_row(role, promise, today) for role, promise in picked]

    @api.model
    def _distri_inicio_row(self, role, promise, today=None):
        promise = promise.sudo()
        partner = promise.partner_id
        document, _phrase = document_of(promise.order_id)
        digits = promise._whatsapp_digits()
        row = {
            "id": promise.id,
            "rol": role,
            "cliente": partner._distri_app_name() or partner.name or "",
            "documento": document,
            "pedido": promise.order_id.name or "",
            "importe": promise.amount,
            "hace": days_ago(promise.delivered_day, today),
            "wa_url": "",
            "wa_post": "",
            "subir_post": "",
            "subir_url": "",
            "foto_url": "",
        }
        if role == "deposito":
            # the same link and upload as /deposito/reclamar
            row["wa_url"] = promise._distri_user_row()["wa_url"]
            row["subir_post"] = "/deposito/reclamar/%s/subir" % promise.id
        elif role == "reparto":
            # the driver writes himself («te entregué»), every open item of
            # the customer, as in the driver app
            if digits:
                same = promise.search([
                    ("driver_id", "=", promise.driver_id.id), ("route_kind", "=", "route"),
                    ("partner_id", "child_of", partner.commercial_partner_id.id),
                    ("state", "in", OPEN_STATES)], order="captured_at, id")
                text = self._distri_customer_claim(same) or promise._distri_driver_claim_text()
                row["wa_url"] = "https://wa.me/%s?text=%s" % (digits, quote(text))
            row["foto_url"] = "/reparto#reclamar"
        else:
            # «Reclamar por WhatsApp» of «Comprobantes pendientes» (it keeps
            # who claimed and when) and the receipt upload of /comprobantes
            if digits:
                row["wa_post"] = "/rendiciones/comprobantes/%s/reclamar" % promise.id
            row["subir_url"] = "/comprobantes?cliente_id=%s" % partner.commercial_partner_id.id
        return row

