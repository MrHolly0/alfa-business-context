#!/bin/sh
set -eu
php bin/schema.php
if [ "${CORE_SEED_DEMO:-1}" = 1 ]; then
    php bin/seed-demo.php
fi
