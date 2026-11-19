-- Sanitize the practice copy distri_practica (practica.distrineuquen.com).
-- Idempotent: running it twice gives the same result.
--   psql -v codigo=1234 -v base_url=https://practica.distrineuquen.com -d distri_practica -f sanitizar.sql
-- Run by reiniciar.sh --renovar-golden inside the PRACTICE Postgres
-- (container odoo-practica-db). It refuses any other database, and refuses
-- to run in a cluster that also holds the production database distri_lab.
--
-- Kept real (owner OK): balances, invoices, payments, products and prices.
-- Replaced: every customer's name, phones, e-mails, CUIT/DNI, address, notes,
-- images, coordinates, WhatsApp numbers; staff users archived without
-- password; every token/secret; outgoing mail; crons; chat contents.
\set ON_ERROR_STOP on

DO $$ BEGIN
  IF current_database() <> 'distri_practica' THEN
    RAISE EXCEPTION 'sanitizar.sql only runs on distri_practica (got %)', current_database();
  END IF;
  IF EXISTS (SELECT 1 FROM pg_database WHERE datname = 'distri_lab') THEN
    RAISE EXCEPTION 'refusing: this Postgres also holds distri_lab (production)';
  END IF;
END $$;

\if :{?codigo}
\else
  \echo 'missing -v codigo=NNNN'
  SELECT 1/0;
\endif
\if :{?base_url}
\else
  \set base_url https://practica.distrineuquen.com
\endif

BEGIN;

-- ------------------------------------------------------------- helpers
CREATE OR REPLACE FUNCTION pg_temp.cuit(base text) RETURNS text LANGUAGE plpgsql IMMUTABLE AS $f$
DECLARE w int[] := '{5,4,3,2,7,6,5,4,3,2}'; s int := 0; i int; r int;
BEGIN
  FOR i IN 1..10 LOOP s := s + substr(base, i, 1)::int * w[i]; END LOOP;
  r := 11 - (s % 11);
  RETURN base || CASE WHEN r = 11 THEN '0' WHEN r = 10 THEN '9' ELSE r::text END;
END $f$;

-- ------------------------------------------------------------- customers
-- Every contact except: the company itself, internal users (staff), and the
-- technical contacts created by modules (they have an XML id: public user,
-- OdooBot, «Pedidos web», generic contacts...).
DROP TABLE IF EXISTS pg_temp.anon;
DROP TABLE IF EXISTS pg_temp.orig_names;
CREATE TEMP TABLE anon AS
SELECT p.id,
       row_number() OVER (ORDER BY p.id) AS n,
       upper(substring(p.name FROM '^\s*(ZC|C|X|Y|Z|W|S)\s*-')) AS pref,
       coalesce(nullif(trim(p.city), ''), 'Neuquén') AS town,
       lower(trim(regexp_replace(p.name, '^\s*(ZC|C|X|Y|Z|W|S)\s*-\s*', '', 'i'))) AS orig
FROM res_partner p
WHERE p.id NOT IN (SELECT partner_id FROM res_company WHERE partner_id IS NOT NULL)
  AND p.id NOT IN (SELECT partner_id FROM res_users WHERE share IS NOT TRUE AND partner_id IS NOT NULL)
  AND p.id NOT IN (SELECT res_id FROM ir_model_data
                   WHERE model = 'res.partner' AND module NOT IN ('__export__', '__import__'))
  AND p.distri_generic_kind IS NULL;

UPDATE res_partner p SET
  name = CASE WHEN a.pref IS NOT NULL THEN a.pref || '-Cliente ' || lpad(a.n::text, 4, '0')
              ELSE 'Cliente ' || lpad(a.n::text, 4, '0') || ' - ' || a.town END,
  email = NULL, email_normalized = NULL,
  phone = CASE WHEN p.phone IS NULL OR p.phone = '' THEN NULL ELSE '299 555-' || lpad(a.n::text, 4, '0') END,
  phone_sanitized = CASE WHEN p.phone IS NULL OR p.phone = '' THEN NULL ELSE '+549299555' || lpad(a.n::text, 4, '0') END,
  distri_whatsapp = CASE WHEN p.distri_whatsapp IS NULL OR p.distri_whatsapp = '' THEN NULL
                         ELSE '549299555' || lpad(a.n::text, 4, '0') END,
  vat = CASE WHEN p.vat IS NULL OR p.vat = '' THEN NULL
             WHEN regexp_replace(p.vat, '\D', '', 'g') ~ '^\d{11}$'
               THEN pg_temp.cuit('20' || lpad((90000000 + a.n)::text, 8, '0'))
             ELSE (90000000 + a.n)::text END,
  street = CASE WHEN p.street IS NULL OR p.street = '' THEN NULL ELSE 'Calle de Práctica ' || (100 + a.n) END,
  street2 = NULL, company_registry = NULL, website = NULL, function = NULL, comment = NULL,
  company_name = NULL, barcode = NULL, l10n_ar_gross_income_number = NULL,
  distri_driver_note = NULL, distri_alta_contacto = NULL, distri_provisorio_nota = NULL,
  distri_uncollectible_note = NULL, picking_warn_msg = NULL, sale_warn_msg = NULL,
  purchase_warn_msg = NULL, peppol_endpoint = NULL, global_location_number = NULL,
  partner_latitude = NULL, partner_longitude = NULL,
  distri_customer_latitude = NULL, distri_customer_longitude = NULL
FROM anon a WHERE a.id = p.id;

-- stored display fields derived from the name
UPDATE res_partner p SET complete_name = p.name FROM anon a WHERE a.id = p.id AND p.parent_id IS NULL;
UPDATE res_partner p SET complete_name = par.name || ', ' || p.name
FROM anon a, res_partner par WHERE a.id = p.id AND par.id = p.parent_id;
UPDATE res_partner p SET commercial_company_name = CASE WHEN c.is_company THEN c.name ELSE NULL END
FROM anon a, res_partner c WHERE a.id = p.id AND c.id = coalesce(p.commercial_partner_id, p.id);

-- free text that quotes customers' names (amounts and links untouched).
-- `orig` are the names before this run; on a second run they are the
-- anonymized ones, so it stays idempotent.
CREATE TEMP TABLE orig_names AS SELECT DISTINCT orig FROM anon WHERE length(orig) >= 8;
UPDATE account_payment SET memo = 'Pago ' || id WHERE memo IS NOT NULL AND memo <> 'Pago ' || id;
UPDATE account_move SET ref = NULL WHERE ref IS NOT NULL AND move_type = 'entry';
UPDATE account_move m SET ref = 'Ref. ' || m.id WHERE m.ref IS NOT NULL
  AND EXISTS (SELECT 1 FROM orig_names o WHERE position(o.orig IN lower(m.ref)) > 0);
UPDATE account_move_line l SET ref = m.ref FROM account_move m
  WHERE m.id = l.move_id AND l.ref IS DISTINCT FROM m.ref;
-- journal entries and credit notes carry typed labels (payer, reason): generic
UPDATE account_move_line l SET name = 'Movimiento ' || m.name FROM account_move m
  WHERE m.id = l.move_id AND m.move_type = 'entry' AND l.name IS DISTINCT FROM 'Movimiento ' || m.name;
UPDATE account_move_line l SET name = 'Nota de crédito (texto borrado en práctica)' FROM account_move m
  WHERE m.id = l.move_id AND m.move_type = 'out_refund' AND l.display_type = 'product';
UPDATE account_move_line l SET name = 'Movimiento (texto borrado en práctica)' WHERE l.name IS NOT NULL
  AND EXISTS (SELECT 1 FROM orig_names o WHERE position(o.orig IN lower(l.name)) > 0);
UPDATE distri_cuenta_movimiento m SET concepto = 'Movimiento (texto borrado en práctica)'
  WHERE m.concepto IS NOT NULL
  AND EXISTS (SELECT 1 FROM orig_names o WHERE position(o.orig IN lower(m.concepto)) > 0);
UPDATE distri_online_payment p SET move_name = 'Pago ' || p.id WHERE p.move_name IS NOT NULL
  AND EXISTS (SELECT 1 FROM orig_names o WHERE position(o.orig IN lower(p.move_name)) > 0);
UPDATE mail_activity SET note = NULL, summary = NULL, res_name = NULL;
UPDATE ir_attachment SET index_content = NULL WHERE index_content IS NOT NULL;

-- staff contacts (names kept): no personal contact data
UPDATE res_partner SET email = NULL, email_normalized = NULL, phone = NULL, phone_sanitized = NULL,
  distri_whatsapp = NULL, vat = NULL, street = NULL, street2 = NULL, comment = NULL,
  partner_latitude = NULL, partner_longitude = NULL
WHERE id IN (SELECT partner_id FROM res_users WHERE share IS NOT TRUE AND partner_id IS NOT NULL)
  AND id NOT IN (SELECT partner_id FROM res_company WHERE partner_id IS NOT NULL);

-- customers' bank accounts (the company's own accounts are kept)
UPDATE res_partner_bank b SET
  acc_number = '00000031' || lpad(b.id::text, 14, '0'),
  sanitized_acc_number = '00000031' || lpad(b.id::text, 14, '0'),
  acc_holder_name = NULL, note = NULL
WHERE b.partner_id NOT IN (SELECT partner_id FROM res_company WHERE partner_id IS NOT NULL);

-- names copied onto documents
UPDATE account_move m SET invoice_partner_display_name = p.name
FROM res_partner p WHERE p.id = m.partner_id AND m.invoice_partner_display_name IS DISTINCT FROM p.name;
UPDATE account_move SET narration = NULL, invoice_source_email = NULL, access_token = NULL,
  sending_data = NULL
WHERE narration IS NOT NULL OR invoice_source_email IS NOT NULL OR access_token IS NOT NULL
   OR sending_data IS NOT NULL;
UPDATE sale_order SET
  distri_original = NULL, distri_nombre = NULL, distri_telefono = NULL, distri_domicilio = NULL,
  distri_encomienda_nombre = NULL, distri_encomienda_dni = NULL, distri_wa_phone = NULL,
  distri_wa_code = NULL, distri_agil_texto = NULL, distri_comentario = NULL,
  distri_nota_deposito = NULL, note = NULL, client_order_ref = NULL, distri_customer_notice = NULL,
  distri_mp_init_point = NULL, distri_mp_preference_id = NULL, distri_mp_reference = NULL,
  signed_by = NULL, access_token = NULL,
  distri_link_token = CASE WHEN distri_link_token IS NULL THEN NULL
                           ELSE md5(id::text || clock_timestamp()::text || random()::text) END;
UPDATE stock_picking SET note = NULL WHERE note IS NOT NULL;
UPDATE purchase_order SET access_token = NULL WHERE access_token IS NOT NULL;
UPDATE ir_attachment SET access_token = NULL WHERE access_token IS NOT NULL;

-- ------------------------------------------------------------- module data with personal text
DELETE FROM distri_whatsapp_message;
DELETE FROM distri_whatsapp_chat_import;
DELETE FROM distri_arca_name;
DELETE FROM distri_route_gps;
DELETE FROM distri_sync_event;
DELETE FROM distri_sync_rejection;
DELETE FROM distri_asignar_cliente;
DELETE FROM distri_importar_pedido;
DELETE FROM distri_rate_hit;
DELETE FROM distri_backup_status;
UPDATE distri_mp_collection SET payer_name = 'Pagador ' || id, payer_cuit = NULL,
  proposal_note = NULL, ignore_note = NULL, conflict_note = NULL, evidence_note = NULL,
  candidate_note = NULL, customer_sources = NULL, sale_detail = NULL, external_reference = NULL,
  online_label = NULL, authorized_conflict = NULL, wholesale_label = NULL;
UPDATE distri_mp_wholesale_rule SET label = 'Mayorista ' || id, note = NULL;
UPDATE distri_mp_outgoing SET payer_name = 'Destinatario ' || id, payer_cuit = NULL;
UPDATE distri_payer_identity SET value = 'practica-' || id, holder = NULL;
UPDATE distri_mp_day SET note = NULL, incomplete_reason = NULL;
UPDATE distri_cash_sheet SET note = NULL;
UPDATE distri_cash_sheet_line l SET client_raw = coalesce(p.name, 'Cliente de planilla'), notes = NULL
FROM distri_cash_sheet_line l2 LEFT JOIN res_partner p ON p.id = l2.partner_id
WHERE l2.id = l.id;
UPDATE distri_online_payment SET memo = NULL;
UPDATE distri_balance_review r SET partner_name = coalesce(p.name, 'Cliente'), summary = NULL,
  audited_note = NULL
FROM distri_balance_review r2 LEFT JOIN res_partner p ON p.id = r2.partner_id WHERE r2.id = r.id;
UPDATE distri_balance_review_line SET note = NULL;
UPDATE distri_balance_adjustment SET motive = 'Ajuste de saldo (texto borrado en práctica)';
UPDATE distri_account_comment SET body = 'Comentario (texto borrado en práctica)';
UPDATE distri_carrier SET phone = '299 555-0999', note = NULL;
UPDATE distri_whatsapp_numero SET name = 'Número de práctica ' || id, phone_number_id = 'practica-' || id;
UPDATE distri_route_case SET candidates = NULL, driver_explanation = NULL, investigation = NULL,
  resolution = NULL;
UPDATE distri_route SET driver_explanation = NULL, departure_note = NULL;
UPDATE distri_route_stop SET comment = NULL;
UPDATE distri_route_expense SET comment = NULL;
UPDATE distri_promised_transfer SET comment = NULL, partial_note = NULL;
UPDATE distri_cash_collection SET note = NULL, memo = NULL;

-- ------------------------------------------------------------- chatter and mail
UPDATE mail_message SET
  body = CASE WHEN body IS NULL OR body = '' THEN body ELSE '<p>(Texto borrado en el entorno de práctica)</p>' END,
  subject = NULL, email_from = NULL, reply_to = NULL, incoming_email_cc = NULL,
  outgoing_email_to = NULL, incoming_email_to = NULL;
UPDATE mail_tracking_value SET old_value_char = NULL, new_value_char = NULL,
  old_value_text = NULL, new_value_text = NULL;
DELETE FROM mail_mail;
DELETE FROM ir_mail_server;
DELETE FROM fetchmail_server;
UPDATE mail_alias_domain SET name = 'practica.invalid', default_from = NULL;
DELETE FROM website_visitor;
DELETE FROM sms_sms;

-- ------------------------------------------------------------- files
-- only the files the practice needs: product images, website, assets...
-- (receipts, WhatsApp media, invoice PDFs, customer images are dropped; the
-- golden filestore then copies only the files still referenced)
DELETE FROM ir_attachment
WHERE res_model IS NOT NULL
  AND res_model NOT IN ('product.template', 'product.product', 'product.category',
                        'product.public.category', 'ir.ui.view', 'ir.ui.menu', 'payment.method',
                        'payment.provider', 'spreadsheet.dashboard', 'website', 'website.page',
                        'res.lang', 'res.company', 'res.country', 'onboarding.onboarding.step',
                        'ir.module.module', 'theme.ir.attachment');

-- ------------------------------------------------------------- users
-- real staff: archived, no password, no 2FA/OAuth. Odoo needs one active
-- administrator: base.user_admin stays active but WITHOUT password (no one
-- can log in as it; maintenance is done with `odoo shell`).
UPDATE res_users SET active = (id IN (SELECT res_id FROM ir_model_data WHERE module = 'base' AND name = 'user_admin')),
  password = NULL, totp_secret = NULL, oauth_access_token = NULL,
  oauth_uid = NULL, oauth_provider_id = NULL, signature = NULL
WHERE id <> 1 AND login NOT LIKE 'practica.%';
-- customers' portal logins were their e-mails
UPDATE res_users SET login = 'cliente.' || id || '@practica.invalid'
WHERE share AND login NOT IN ('public', 'portaltemplate') AND login NOT LIKE 'cliente.%@practica.invalid';
DELETE FROM res_users_apikeys;
DELETE FROM auth_passkey_key;
DELETE FROM auth_totp_device;
DELETE FROM res_users_log;
DELETE FROM res_device_log;
DELETE FROM res_device;
DELETE FROM mail_push_device;
DELETE FROM mail_push;
DELETE FROM mail_presence;
DELETE FROM bus_bus;
DELETE FROM ir_logging;
DELETE FROM res_config_settings;
UPDATE auth_oauth_provider SET client_id = NULL, enabled = false;
DELETE FROM iap_account;
UPDATE website SET domain = :'base_url', google_analytics_key = NULL, google_maps_api_key = NULL,
  plausible_shared_key = NULL, google_search_console = NULL;

-- ------------------------------------------------------------- crons: none
UPDATE ir_cron SET active = false WHERE active;

-- ------------------------------------------------------------- system parameters
-- blank every token / secret (also the ones a future version may add)
UPDATE ir_config_parameter SET value = ''
WHERE key ~* '(token|secret|password|passwd|api_?key|client_id|client_identifier|webhook)'
  AND key NOT IN ('database.secret');
INSERT INTO ir_config_parameter (key, value, create_uid, write_uid, create_date, write_date)
SELECT k, '', 1, 1, now() AT TIME ZONE 'utc', now() AT TIME ZONE 'utc'
FROM unnest(ARRAY['distri_pedido.wa_access_token', 'distri_pedido.wa_app_secret',
                  'distri_pedido.wa_verify_token', 'distri_pedido.wa_phone_number_id',
                  'distri_pedido.wa_waba_id', 'distri_pedido.mp_access_token',
                  'distri_pedido.mp_client_secret', 'distri_pedido.mp_webhook_secret',
                  'distri_pedido.mp_oauth_read_token', 'distri_pedido.mp_oauth_read_user_id',
                  'distri_pedido.mp_oauth_pending', 'iap_vies.client_token',
                  'iap_vies.client_identifier', 'auth_oauth.google_client_id']) AS k
ON CONFLICT (key) DO UPDATE SET value = '', write_date = now() AT TIME ZONE 'utc';

INSERT INTO ir_config_parameter (key, value, create_uid, write_uid, create_date, write_date)
SELECT k, v, 1, 1, now() AT TIME ZONE 'utc', now() AT TIME ZONE 'utc'
FROM (VALUES
  ('auth_oauth.google_enabled', 'False'),
  ('distri_pedido.mp_enabled', '0'),
  ('distri_pedido.mp_link_habilitado', '0'),
  ('distri_pedido.mp_carpeta_reportes', '/nonexistent/practica'),
  ('distri_pedido.agil_url', 'http://127.0.0.1:9/'),
  ('distri_pedido.catalogo_agil', '0'),
  ('distri_pedido.agilpedido_wa_auto', '0'),
  ('distri_pedido.revendedor_codigo', md5(random()::text || clock_timestamp()::text)),
  ('distri_pedido.alias_titular', 'Distri Neuquén (práctica)'),
  ('distri_pedido.alias_transferencia', 'PRACTICA.DISTRI.NQN'),
  ('distri_pedido.telefono_empresa', '299 555-0000'),
  ('distri_pedido.whatsapp_empresa', '5492995550000'),
  ('web.base.url', :'base_url'),
  ('web.base.url.freeze', 'True'),
  -- wkhtmltopdf reads report assets from the container itself
  ('report.url', 'http://127.0.0.1:8069'),
  ('database.uuid', gen_random_uuid()::text),
  ('database.secret', gen_random_uuid()::text || gen_random_uuid()::text),
  ('database.is_neutralized', 'True'),
  ('distri_practica.activo', '1'),
  ('distri_practica.codigo', :'codigo')
) AS t(k, v)
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, write_date = now() AT TIME ZONE 'utc';

COMMIT;

-- report (no secret values printed)
SELECT 'clientes anonimizados' AS que, count(*)::text AS valor FROM anon
UNION ALL SELECT 'usuarios activos', string_agg(login, ', ' ORDER BY login) FROM res_users WHERE active
UNION ALL SELECT 'crons activos', count(*)::text FROM ir_cron WHERE active
UNION ALL SELECT 'servidores de correo', count(*)::text FROM ir_mail_server
UNION ALL SELECT 'adjuntos', count(*)::text FROM ir_attachment;
