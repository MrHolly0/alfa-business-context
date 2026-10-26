#!/bin/sh
# Additive organizer-case setup. Existing .env and user contexts are preserved.
set -eu
cd "$(dirname "$0")/.."
ROOT_DIR=$(pwd)
CASE_DATA_DIR=${CASE_DATA_DIR:-"$ROOT_DIR/.local/data/case"}
CASE_DATA_DIR=$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$CASE_DATA_DIR")
export CASE_DATA_DIR
COMPOSE_OVERRIDE_FILE=${COMPOSE_OVERRIDE_FILE:-}
if [ -n "$COMPOSE_OVERRIDE_FILE" ] && [ ! -f "$COMPOSE_OVERRIDE_FILE" ]; then
  echo 'COMPOSE_OVERRIDE_FILE must name an existing Compose file.' >&2
  exit 2
fi
if [ -n "$COMPOSE_OVERRIDE_FILE" ]; then
  compose_base() { docker compose -f compose.yaml -f "$COMPOSE_OVERRIDE_FILE" "$@"; }
  compose_case() { docker compose -f compose.yaml -f compose.judging.yaml -f "$COMPOSE_OVERRIDE_FILE" "$@"; }
else
  compose_base() { docker compose -f compose.yaml "$@"; }
  compose_case() { docker compose -f compose.yaml -f compose.judging.yaml "$@"; }
fi
if [ "${1:-}" != --allow-organizer-data-to-alfa ]; then
  echo 'Usage: sh infra/prepare-judging.sh --allow-organizer-data-to-alfa' >&2
  echo 'The flag authorizes only the supplied organizer CSV profiles and exact imported PDF revisions through Alfa.' >&2
  exit 2
fi
umask 077
export LOCAL_UID="$(id -u)" LOCAL_GID="$(id -g)"
if ! compose_case --profile tools build case-key-check >/dev/null 2>&1; then
  echo 'Could not build the offline provider-key preflight; check Docker and provider setup.' >&2
  exit 1
fi
key_status=
if key_status="$(compose_case --profile tools run --rm --no-deps case-key-check 2>/dev/null)"; then
  :
else
  echo 'Could not verify HACKATHON_API_KEY configuration; check Docker Compose and provider setup.' >&2
  exit 1
fi
if [ "$key_status" = missing ]; then
  echo 'HACKATHON_API_KEY is missing; configure the approved Alfa gateway before preparing judging data.' >&2
  exit 1
fi
if [ "$key_status" != configured ]; then
  echo 'Unexpected response from provider-key preflight; stopping before case preparation.' >&2
  exit 1
fi
mkdir -p "$CASE_DATA_DIR"
python3 infra/download-case.py --case-dir "$CASE_DATA_DIR"
compose_base up --build --wait
compose_case --profile tools build core-case-import intelligence
compose_case --profile tools run --rm --no-deps core-case-import
compose_case --profile tools run --rm --no-deps core-case-import bin/case-company-map.php organizer-2026-v1 U_57706 U_9250 U_408263 > "$CASE_DATA_DIR/core-company-map.json.tmp"
chmod 600 "$CASE_DATA_DIR/core-company-map.json.tmp"
mv "$CASE_DATA_DIR/core-company-map.json.tmp" "$CASE_DATA_DIR/core-company-map.json"
if [ ! -f "$CASE_DATA_DIR/ocr-manifest.json" ]; then
  compose_case --profile tools run --build --rm --no-deps case-ocr > "$CASE_DATA_DIR/ocr-check.json"
fi
python3 infra/prepare-case-manifest.py --case-dir "$CASE_DATA_DIR"
compose_case --profile tools run --build --rm --no-deps case-volume-setup
compose_case --profile tools run --rm --no-deps -e HACKATHON_API_KEY= -e OPENROUTER_API_KEY= intelligence case-import \
  --manifest /case/manifest.json --state-dir /app/.state --output case-bundle.json > "$CASE_DATA_DIR/import-check.json"
chmod 600 "$CASE_DATA_DIR/import-check.json"
compose_case --profile tools run --rm --no-deps --entrypoint cat intelligence /case/case-bundle.json > "$CASE_DATA_DIR/case-bundle.json"
chmod 600 "$CASE_DATA_DIR/case-bundle.json"
python3 infra/prepare-case-manifest.py --case-dir "$CASE_DATA_DIR" --bundle
compose_case --profile tools run --rm --no-deps case-volume-setup
compose_case --profile tools run --rm --no-deps core-case-import \
  bin/prepare-judging.php /case/core-setup.json "${JUDGING_USER_EMAIL:-demo@alpha.test}"
compose_case up --wait
printf 'Ready: http://localhost:%s — choose one of the three organizer companies.\n' "${WEB_PORT:-4173}"
