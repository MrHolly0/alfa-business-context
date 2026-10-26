#!/bin/sh
# Additive organizer-case setup. Existing .env and user contexts are preserved.
set -eu
cd "$(dirname "$0")/.."
if [ "${1:-}" != --allow-organizer-data-to-alfa ]; then
  echo 'Usage: sh infra/prepare-judging.sh --allow-organizer-data-to-alfa' >&2
  echo 'The flag authorizes only the supplied organizer CSV profiles and exact imported PDF revisions through Alfa.' >&2
  exit 2
fi
umask 077
export LOCAL_UID="$(id -u)" LOCAL_GID="$(id -g)"
python3 infra/download-case.py
docker compose up --build --wait
docker compose -f compose.yaml -f compose.judging.yaml --profile tools build core-case-import intelligence
docker compose -f compose.yaml -f compose.judging.yaml --profile tools run --rm --no-deps core-case-import
docker compose -f compose.yaml -f compose.judging.yaml --profile tools run --rm --no-deps core-case-import bin/case-company-map.php organizer-2026-v1 U_57706 U_9250 U_408263 > .local/data/case/core-company-map.json.tmp
mv .local/data/case/core-company-map.json.tmp .local/data/case/core-company-map.json
if [ ! -f .local/data/case/ocr-manifest.json ]; then
  docker run --rm --network none --read-only --tmpfs /tmp:rw,nosuid,nodev,size=256m \
    --user "$(id -u):$(id -g)" --mount "type=bind,src=$(pwd)/.local/data/case,dst=/case" \
    alfa-business-context-intelligence case-ocr --case-dir /case > .local/data/case/ocr-check.json
fi
python3 infra/prepare-case-manifest.py
docker compose -f compose.yaml -f compose.judging.yaml --profile tools run --rm --no-deps case-volume-setup
docker compose -f compose.yaml -f compose.judging.yaml --profile tools run --rm --no-deps \
  -e HACKATHON_API_KEY= -e OPENROUTER_API_KEY= intelligence case-import --manifest /case/manifest.json --state-dir /app/.state --output case-bundle.json \
  > .local/data/case/import-check.json
docker compose -f compose.yaml -f compose.judging.yaml --profile tools run --rm --no-deps --entrypoint cat intelligence /case/case-bundle.json > .local/data/case/case-bundle.json
python3 infra/prepare-case-manifest.py --bundle
docker compose -f compose.yaml -f compose.judging.yaml --profile tools run --rm --no-deps case-volume-setup
docker compose -f compose.yaml -f compose.judging.yaml --profile tools run --rm --no-deps core-case-import \
  bin/prepare-judging.php /case/core-setup.json "${JUDGING_USER_EMAIL:-demo@alpha.test}"
docker compose -f compose.yaml -f compose.judging.yaml up --build --wait
printf '%s\n' 'Ready: http://localhost:4173 — choose one of the three organizer companies.'
