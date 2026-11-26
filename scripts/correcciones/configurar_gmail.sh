#!/usr/bin/env bash
# Outgoing mail through the company's Gmail account (decision E, owner
# 26/11/2026): password reset links and other system mail.
# It asks for the Gmail APP PASSWORD (myaccount.google.com/apppasswords, needs
# 2-step verification) without echoing it; the password travels only through
# the pipe into odoo shell, never in argv or a file.
# It creates or updates one ir.mail_server (smtp.gmail.com:587 STARTTLS),
# sets mail.default.from to the account, and tests the connection.
# The mails already in «exception» stay as they are (never resent).
#   scripts/correcciones/configurar_gmail.sh <base> <account>
set -euo pipefail
DB=${1:?usage: $0 <base> <account>}
ACCOUNT=${2:?usage: $0 <base> <account>}
[[ "$DB" =~ ^[A-Za-z0-9_]+$ ]] || { echo "bad base name $DB" >&2; exit 2; }
[[ "$ACCOUNT" =~ ^[A-Za-z0-9._%+-]+@gmail\.com$ ]] || { echo "not a gmail address: $ACCOUNT" >&2; exit 2; }
HERE=$(cd "$(dirname "$0")" && pwd)
read -rsp "Contraseña de aplicación de Gmail para $ACCOUNT: " PW; echo
PW=${PW// /}
[[ ${#PW} -eq 16 ]] || { echo "the app password has 16 letters (got ${#PW})" >&2; exit 2; }
PW_HEX=$(printf '%s' "$PW" | od -An -tx1 | tr -d ' \n')
unset PW

PY=$(cat <<PY
env = env(user=2)
password = bytes.fromhex("$PW_HEX").decode()
Server = env["ir.mail_server"].sudo()
vals = {"name": "Gmail $ACCOUNT", "smtp_host": "smtp.gmail.com", "smtp_port": 587,
        "smtp_encryption": "starttls", "smtp_authentication": "login",
        "smtp_user": "$ACCOUNT", "smtp_pass": password, "from_filter": "$ACCOUNT",
        "sequence": 1, "active": True}
server = Server.search([("smtp_user", "=", "$ACCOUNT")], limit=1)
server = server.write(vals) and server if server else Server.create(vals)
try:
    server.test_smtp_connection()  # Odoo 19: raises UserError on failure
except Exception as error:
    env.cr.rollback()
    message = str(getattr(error, "args", [error])[0]) or repr(error)
    print("MAIL ERROR", message.splitlines()[0][:200])
    raise SystemExit(1)
env["ir.config_parameter"].sudo().set_param("mail.default.from", "$ACCOUNT")
env.cr.commit()
print("MAIL OK", server.id, server.name)
PY
)
if [[ "$DB" == distri_practica ]]; then
  # shellcheck source=../../practica/comun.sh
  source "$HERE/../../practica/comun.sh"
  odoo_run shell -d "$DB" --no-http --log-level=warn <<<"$PY" 2>&1 | rg -e '^MAIL' -e 'Traceback' -e 'Error'
else
  NAME=distri-gmail-$$ "$HERE/../l10n_ar/lab.sh" odoo shell -d "$DB" --no-http \
    --log-level=warn <<<"$PY" 2>&1 | rg -e '^MAIL' -e 'Traceback' -e 'Error'
fi
