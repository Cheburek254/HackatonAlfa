#!/bin/sh
set -eu

envsubst '${ALFA_HTTPS_HOST} ${ALFA_HTTPS_PORT}' \
    < /etc/alfahack/https.conf.template > /etc/nginx/conf.d/default.conf
nginx -t

certificate="/etc/letsencrypt/live/${ALFA_HTTPS_HOST}/fullchain.pem"
(
    active_certificate=$(readlink "$certificate")
    while sleep 60; do
        renewed_certificate=$(readlink "$certificate") || continue
        if [ "$renewed_certificate" != "$active_certificate" ] && nginx -t && nginx -s reload; then
            active_certificate=$renewed_certificate
        fi
    done
) &

exec nginx -g 'daemon off;'
