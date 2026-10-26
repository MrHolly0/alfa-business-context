#!/bin/sh
set -eu

secret_file=/var/lib/core-secrets/app-secret
if [ -z "${APP_SECRET:-}" ]; then
    if [ ! -s "$secret_file" ]; then
        umask 077
        php -r 'echo bin2hex(random_bytes(32));' > "${secret_file}.new"
        mv "${secret_file}.new" "$secret_file"
    fi
    APP_SECRET=$(cat "$secret_file")
    export APP_SECRET
fi

mkdir -p "$APACHE_RUN_DIR" /tmp/core/var
exec "$@"
