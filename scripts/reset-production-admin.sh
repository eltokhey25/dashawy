#!/usr/bin/env bash
# Reset an admin password on the production (Neon) database.
#
# Usage: scripts/reset-production-admin.sh [email]
# Prompts for the new password without echoing it, pulls DATABASE_URL from the
# linked Vercel project, resets the password, clears login lockouts, and
# deletes the pulled credentials afterwards.
set -euo pipefail

cd "$(dirname "$0")/.."
PY=.venv/bin/python

EMAIL="${1:-}"
if [ -z "$EMAIL" ]; then
  read -rp "Admin email: " EMAIL
fi

ENV_FILE="$(mktemp)"
trap 'rm -f "$ENV_FILE"' EXIT
vercel env pull "$ENV_FILE" --environment=production --yes >/dev/null

DATABASE_URL="$("$PY" -c "from dotenv import dotenv_values as d; import sys; print(d(sys.argv[1])['DATABASE_URL_UNPOOLED'])" "$ENV_FILE")"
export DATABASE_URL

ADMIN_EMAIL="$EMAIL" "$PY" - <<'EOF'
import getpass
import os

import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'dashawy_backend.settings')
django.setup()

from django.core.management import call_command

email = os.environ['ADMIN_EMAIL']
password = getpass.getpass('New password (8+ chars, letters and numbers): ')
if password != getpass.getpass('Repeat new password: '):
    raise SystemExit('Passwords do not match; nothing changed.')
call_command('reset_admin_password', email=email, password=password)
call_command('axes_reset')
EOF

echo "Done. Sign in at https://dashawy.vercel.app/admin"
