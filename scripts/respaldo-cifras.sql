-- Key figures of distri_lab, one "name|value" per line. respaldo-diario.sh
-- runs it inside the same snapshot as pg_dump; respaldo-probar.sh runs it on
-- the restored copy and both outputs must be identical.
select 'clientes_activos|' || count(*) from res_partner where active;
select 'contactos_total|' || count(*) from res_partner;
select 'facturas_publicadas|' || count(*) || '|' || coalesce(sum(amount_total), 0)
  from account_move where move_type in ('out_invoice', 'out_refund') and state = 'posted';
select 'asientos_total|' || count(*) from account_move;
select 'pagos|' || count(*) || '|' || coalesce(sum(amount), 0) from account_payment;
select 'saldo_deudores|' || coalesce(sum(l.balance), 0)
  from account_move_line l join account_account a on a.id = l.account_id
  where a.account_type = 'asset_receivable' and l.parent_state = 'posted';
select 'pedidos|' || count(*) || '|' || coalesce(sum(amount_total), 0) from sale_order;
select 'productos|' || count(*) from product_product;
select 'adjuntos|' || count(*) from ir_attachment;
-- the imported history, the same digest as scripts/l10n_ar/snap.py
select 'historial|' || count(*) || '|' || coalesce(sum(debe), 0) || '|' || coalesce(sum(haber), 0)
  || '|' || coalesce(md5(string_agg(concat_ws('|', id, partner_id, fecha, comprobante, concepto,
  tipo, debe, haber, currency_id, origen_id), ',' order by id)), '')
  from distri_cuenta_movimiento;
select 'modulo|' || coalesce(latest_version, '') from ir_module_module where name = 'distri_pedido';
-- rows of every table
select 'tabla:' || table_name || '|' || (xpath('/row/c/text()', query_to_xml(
  format('select count(*) as c from %I.%I', table_schema, table_name), false, true, '')))[1]::text
  from information_schema.tables
  where table_schema = 'public' and table_type = 'BASE TABLE' order by table_name;
