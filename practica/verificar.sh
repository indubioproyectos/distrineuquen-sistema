#!/usr/bin/env bash
# Leak check of the practice database distri_practica against production.
#   ./verificar.sh            exit 0 = clean, 1 = something real survived
# Takes from production (READ ONLY, distri_lab) every customer's name, phone,
# WhatsApp, e-mail, CUIT/DNI and street, and the hash of every secret system
# parameter, and the company's own phone/WhatsApp lines (except the practice
# line 2994646085); then searches ALL text columns of distri_practica for them.
# Prints only table.column and counts, never a value. The samples live in a
# temp table of one psql session and in a 600 file deleted on exit.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
PROD_PSQL=(docker exec -i odoo-lab-db-1 psql -U odoo -d distri_lab -X -q -At -v ON_ERROR_STOP=1)
PRAC_PSQL=(docker exec -i odoo-practica-db psql -U odoo -d distri_practica -X -q -At -v ON_ERROR_STOP=1)
umask 077
TMP=$(mktemp -d "$HERE/work/verificar.XXXXXX")
trap 'rm -rf "$TMP"' EXIT

# ---- samples from production (read only)
"${PROD_PSQL[@]}" > "$TMP/muestras.tsv" <<'SQL'
BEGIN TRANSACTION READ ONLY;
COPY (
  WITH c AS (
    SELECT p.* FROM res_partner p
    WHERE p.id NOT IN (SELECT partner_id FROM res_company WHERE partner_id IS NOT NULL)
      AND p.id NOT IN (SELECT partner_id FROM res_users WHERE share IS NOT TRUE AND partner_id IS NOT NULL)
      AND p.id NOT IN (SELECT res_id FROM ir_model_data WHERE model = 'res.partner'
                       AND module NOT IN ('__export__', '__import__'))
      AND p.distri_generic_kind IS NULL
  ), v AS (
    SELECT 'text' AS kind, lower(trim(regexp_replace(name, '^\s*(ZC|C|X|Y|Z|W|S)\s*-\s*', '', 'i'))) AS val FROM c
    UNION SELECT 'text', lower(trim(email)) FROM c
    UNION SELECT 'text', lower(trim(street)) FROM c WHERE street ~ '\d'
    UNION SELECT 'digits', right(regexp_replace(phone, '\D', '', 'g'), 8) FROM c
    UNION SELECT 'digits', right(regexp_replace(distri_whatsapp, '\D', '', 'g'), 8) FROM c
    UNION SELECT 'digits', regexp_replace(vat, '\D', '', 'g') FROM c
  )
  SELECT kind, val FROM v
  -- a town is not a customer: names equal to a city/zone are not samples
  WHERE val NOT IN (SELECT lower(trim(city)) FROM res_partner WHERE city IS NOT NULL
                    UNION SELECT lower(name) FROM res_country_state
                    UNION SELECT lower(name) FROM distri_zona)
    AND (kind = 'text' AND length(val) >= 8) OR (kind = 'digits' AND length(val) >= 7)
) TO STDOUT;
COMMIT;
SQL
# secrets: key + md5 of the production value (never the value)
"${PROD_PSQL[@]}" > "$TMP/secretos.tsv" <<'SQL'
COPY (SELECT key, md5(value) FROM ir_config_parameter
      WHERE value <> '' AND (key ~* '(token|secret|password|passwd|api_?key|client_id|client_identifier|webhook|verify|waba|phone_number_id)'
                             OR key IN ('database.uuid', 'distri_pedido.revendedor_codigo'))) TO STDOUT;
SQL
# the company's own lines (public phone, WhatsApp that receives the orders):
# last 8 digits only. The practice line (PRACTICA_LINEA, set by sanitizar.sql)
# is never a production sample: production stopped using it.
PRACTICA_LINEA=94646085
"${PROD_PSQL[@]}" > "$TMP/empresa.tsv" <<SQL
COPY (SELECT DISTINCT right(regexp_replace(value, '\D', '', 'g'), 8) FROM ir_config_parameter
      WHERE (key IN ('distri_pedido.telefono_empresa', 'distri_pedido.whatsapp_empresa')
             OR key ~* '(telefono|whatsapp)_empresa')
        AND length(regexp_replace(value, '\D', '', 'g')) >= 8
        AND right(regexp_replace(value, '\D', '', 'g'), 8) <> '$PRACTICA_LINEA') TO STDOUT;
SQL
echo "muestras de produccion: $(wc -l < "$TMP/muestras.tsv") valores, $(wc -l < "$TMP/secretos.tsv") secretos, $(wc -l < "$TMP/empresa.tsv") lineas de la empresa"

# ---- search in the practice DB
{
  cat <<'SQL'
CREATE TEMP TABLE leak_samples (kind text, val text);
CREATE TEMP TABLE leak_secrets (key text, h text);
CREATE TEMP TABLE leak_company (val text);
COPY leak_samples FROM STDIN;
SQL
  cat "$TMP/muestras.tsv"; echo '\.'
  echo 'COPY leak_secrets FROM STDIN;'
  cat "$TMP/secretos.tsv"; echo '\.'
  echo 'COPY leak_company FROM STDIN;'
  cat "$TMP/empresa.tsv"; echo '\.'
  # the company lines: in every column the customers are searched in, and below also in the parameters and views
  echo "INSERT INTO leak_samples SELECT 'digits', val FROM leak_company;"
  echo "SELECT set_config('leak.diag', '${VERIFICAR_DIAG:-0}', false) \\g /dev/null"
  cat <<'SQL'
CREATE INDEX ON leak_samples (kind);
CREATE TEMP TABLE leak_hits (col text, kind text, hits bigint);
CREATE TEMP TABLE leak_diag (col text, val text, hits bigint);
DO $$
DECLARE r record; n bigint;
BEGIN
  IF current_database() <> 'distri_practica' THEN RAISE EXCEPTION 'wrong database'; END IF;
  FOR r IN
    SELECT c.table_name, c.column_name, c.data_type
    FROM information_schema.columns c
    JOIN information_schema.tables t ON t.table_schema = c.table_schema AND t.table_name = c.table_name
    WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE'
      AND c.data_type IN ('character varying', 'text', 'jsonb')
      AND c.table_name NOT IN ('ir_ui_view', 'ir_model', 'ir_model_fields', 'ir_model_fields_selection',
            'ir_model_data', 'ir_module_module', 'ir_module_module_dependency', 'ir_asset',
            'ir_ui_menu', 'res_lang', 'res_country', 'res_country_state', 'ir_model_constraint',
            'ir_model_relation', 'ir_model_access', 'ir_rule', 'ir_act_window', 'ir_act_server',
            'ir_act_report_xml', 'ir_actions', 'ir_act_client', 'ir_act_window_view', 'mail_template',
            'ir_config_parameter', 'res_currency', 'l10n_latam_identification_type', 'account_tax',
            'account_tax_group', 'account_account', 'discuss_channel', 'distri_zona')
      AND c.column_name NOT IN ('arch_db', 'arch_prev', 'arch_fs')
  LOOP
    -- distinct values first: most columns repeat a handful of values
    EXECUTE format('CREATE TEMP TABLE leak_vals ON COMMIT DROP AS SELECT DISTINCT lower(t.%I::text) AS v '
                   'FROM %I t WHERE t.%I IS NOT NULL AND length(t.%I::text) >= 7',
                   r.column_name, r.table_name, r.column_name, r.column_name);
    SELECT count(*) INTO n FROM leak_vals v
      WHERE EXISTS (SELECT 1 FROM leak_samples s WHERE s.kind = 'text' AND position(s.val IN v.v) > 0);
    IF n > 0 THEN INSERT INTO leak_hits VALUES (r.table_name || '.' || r.column_name, 'texto', n);
      IF current_setting('leak.diag', true) = '1' THEN
        INSERT INTO leak_diag SELECT r.table_name || '.' || r.column_name, s.val, count(*)
        FROM leak_vals v JOIN leak_samples s ON s.kind = 'text' AND position(s.val IN v.v) > 0 GROUP BY 2;
      END IF;
    END IF;
    SELECT count(*) INTO n FROM (SELECT DISTINCT regexp_replace(v, '\D', '', 'g') AS d FROM leak_vals
                                 WHERE v ~ '\d{7}') v
      WHERE EXISTS (SELECT 1 FROM leak_samples s WHERE s.kind = 'digits' AND position(s.val IN v.d) > 0);
    IF n > 0 THEN INSERT INTO leak_hits VALUES (r.table_name || '.' || r.column_name, 'numero', n); END IF;
    DROP TABLE leak_vals;
  END LOOP;
END $$;
\echo '== columnas con datos reales de clientes (debe estar vacio) =='
SELECT col, kind, hits FROM leak_hits ORDER BY col;
SELECT col, val, hits FROM leak_diag WHERE current_setting('leak.diag', true) = '1' ORDER BY col, hits DESC;
\echo '== secretos de produccion que sobrevivieron (debe estar vacio) =='
SELECT p.key FROM ir_config_parameter p JOIN leak_secrets s ON s.key = p.key AND s.h = md5(p.value)
WHERE p.value <> '';
\echo '== lineas de la empresa de produccion en parametros o vistas (debe estar vacio) =='
CREATE TEMP TABLE leak_company_hits AS
SELECT 'ir_config_parameter.' || p.key AS donde FROM ir_config_parameter p JOIN leak_company c
  ON position(c.val IN regexp_replace(p.value, '\D', '', 'g')) > 0
UNION ALL
SELECT 'ir_ui_view.' || v.id FROM ir_ui_view v JOIN leak_company c
  ON position(c.val IN regexp_replace(v.arch_db::text, '\D', '', 'g')) > 0;
SELECT donde FROM leak_company_hits ORDER BY 1;
\echo '== estado =='
SELECT 'usuarios activos no de practica con clave', count(*) FROM res_users WHERE active AND login NOT LIKE 'practica.%' AND id <> 1 AND password IS NOT NULL
UNION ALL SELECT 'crons activos', count(*) FROM ir_cron WHERE active
UNION ALL SELECT 'servidores de correo', count(*) FROM ir_mail_server
UNION ALL SELECT 'activo=1', count(*) FROM ir_config_parameter WHERE key = 'distri_practica.activo' AND value = '1'
UNION ALL SELECT 'base_url practica', count(*) FROM ir_config_parameter WHERE key = 'web.base.url' AND value = 'https://practica.distrineuquen.com';
SELECT CASE WHEN (SELECT count(*) FROM leak_hits) = 0
  AND NOT EXISTS (SELECT 1 FROM ir_config_parameter p JOIN leak_secrets s ON s.key = p.key AND s.h = md5(p.value) WHERE p.value <> '')
  AND NOT EXISTS (SELECT 1 FROM leak_company_hits)
  AND NOT EXISTS (SELECT 1 FROM res_users WHERE active AND login NOT LIKE 'practica.%' AND id <> 1 AND password IS NOT NULL)
  AND NOT EXISTS (SELECT 1 FROM ir_cron WHERE active)
  AND NOT EXISTS (SELECT 1 FROM ir_mail_server)
  THEN 'VERIFICACION_OK' ELSE 'VERIFICACION_FALLO' END;
SQL
} | "${PRAC_PSQL[@]}" | tee "$TMP/resultado.txt"
rg -q '^VERIFICACION_OK$' "$TMP/resultado.txt"
