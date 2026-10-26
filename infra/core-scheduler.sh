#!/bin/sh
# Call the existing Core scheduler. This process owns no separate queue.
set -eu
interval="${CORE_SCHEDULER_INTERVAL:-60}"
case "$interval" in *[!0-9]*|'') echo 'Invalid scheduler interval' >&2; exit 2;; esac
if [ "$interval" -lt 30 ] || [ "$interval" -gt 3600 ]; then
    echo 'Scheduler interval must be 30..3600 seconds' >&2; exit 2
fi
trap 'exit 0' TERM INT
while :; do
    php bin/run-due-news.php || echo 'Core news check failed; see Core run status' >&2
    sleep "$interval" &
    wait "$!" || true
done
