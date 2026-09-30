#!/usr/bin/env bash
# One-time (and safe to repeat) setup to run ON THE SERVER after deploying the latest main:
#
#     cd /opt/barkandambrosiagallery && bash scripts/post_deploy_setup.sh
#
# It never deletes data. It (1) checks the email settings that "Request access" needs and offers to add
# what is missing to .env.prod, (2) loads the published interactions dataset into the database (safe to
# repeat), and (3) shows who can approve access requests.
set -euo pipefail

ENV_FILE="${ENV_FILE:-.env.prod}"
COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.prod.yml)
manage() { "${COMPOSE[@]}" exec -T web pixi run python manage.py "$@"; }

[ -f "$ENV_FILE" ] || { echo "Run this from the project folder (no $ENV_FILE here)."; exit 1; }
has() { grep -Eq "^$1=.+" "$ENV_FILE"; }

echo "== 1. Email (needed for 'Request access')"
missing=()
for key in EMAIL_HOST EMAIL_HOST_USER EMAIL_HOST_PASSWORD DEFAULT_FROM_EMAIL; do
  has "$key" || missing+=("$key")
done
if [ ${#missing[@]} -eq 0 ]; then
  echo "Email settings are present in $ENV_FILE."
else
  echo "Missing in $ENV_FILE: ${missing[*]}"
  echo "You need an SMTP account (your university mail, Gmail with an app password, SendGrid...)."
  read -r -p "Add them now? [y/N] " answer
  if [[ "$answer" =~ ^[Yy]$ ]]; then
    for key in "${missing[@]}"; do
      if [ "$key" = EMAIL_HOST_PASSWORD ]; then read -r -s -p "$key: " value; echo; else read -r -p "$key: " value; fi
      printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
    done
    has EMAIL_PORT || echo "EMAIL_PORT=587" >> "$ENV_FILE"
    echo "Saved. Restarting the web container to pick them up..."
    "${COMPOSE[@]}" up -d web
  else
    echo "Skipped. Until this is set, access-request emails are only written to the log."
  fi
fi

echo
echo "== 2. Published interactions dataset -> database (safe to repeat; never touches accepted or uploaded rows)"
manage import_pathogen_interactions

echo
echo "== 3. Who can approve access requests (every superuser can)"
manage shell -c "
from django.contrib.auth import get_user_model
for u in get_user_model().objects.filter(is_superuser=True, is_active=True):
    print(' -', u.username, u.email or '(no email: they will not get notifications)')
"
echo "To make someone a superuser: sign in as a superuser -> My Account -> edit the user -> role Superuser."
# echo "Access-request emails also go to ACCESS_REQUEST_RECIPIENTS (default gmarais@ufl.edu,hulcr@ufl.edu)."
echo "Access-request emails also go to ACCESS_REQUEST_RECIPIENTS (default gmarais@ufl.edu)."
echo
echo "Done."
