FROM php:8.4-apache AS base

RUN apt-get update \
    && apt-get install -y --no-install-recommends libonig-dev libpq-dev unzip \
    && docker-php-ext-install -j"$(nproc)" mbstring pdo_pgsql \
    && a2enmod rewrite \
    && rm -rf /var/lib/apt/lists/*

ENV APACHE_RUN_DIR=/tmp/apache2 \
    APACHE_PID_FILE=/tmp/apache2/apache2.pid \
    APACHE_LOCK_DIR=/tmp/apache2 \
    APACHE_LOG_DIR=/tmp/apache2 \
    APP_ENV=prod \
    APP_DEBUG=0

WORKDIR /var/www/html

FROM base AS vendor
COPY --from=composer:2 /usr/bin/composer /usr/bin/composer
COPY apps/core/ ./
RUN composer install --no-dev --no-interaction --no-progress --no-scripts --classmap-authoritative

FROM base AS app
WORKDIR /var/www/html
COPY --from=vendor /var/www/html/vendor ./vendor
COPY apps/core/ ./
COPY infra/core/entrypoint.sh /usr/local/bin/core-entrypoint
COPY infra/core/000-default.conf /etc/apache2/sites-available/000-default.conf
COPY infra/core/ports.conf /etc/apache2/ports.conf
COPY infra/core/php.ini /usr/local/etc/php/conf.d/zz-core.ini
RUN chmod 0755 /usr/local/bin/core-entrypoint \
    && mkdir -p /var/lib/core-secrets /var/lib/core-sessions /tmp/apache2 /tmp/core/var \
    && chown -R www-data:www-data /var/lib/core-secrets /var/lib/core-sessions /tmp/apache2 /tmp/core \
    && rm -rf var \
    && ln -s /tmp/core/var var \
    && chown -h www-data:www-data var

FROM app AS runtime
USER www-data
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/core-entrypoint"]
CMD ["apache2ctl", "-D", "FOREGROUND"]

FROM app AS test
USER www-data
CMD ["php", "tests/run.php"]
