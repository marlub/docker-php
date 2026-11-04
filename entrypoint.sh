#!/bin/sh
set -e

if [ ! -f /tmp/.extensions-disabled ]; then
    touch /tmp/.extensions-disabled

    INI_FILES=$(php --ini | awk '/\.ini/ {print $NF}' | tr -d ',' | sort -u)

    for ext in $EXTENSIONS; do
        VAR="PHP_EXTENSION_$(echo "$ext" | tr '[:lower:]' '[:upper:]')"
        VALUE=$(eval echo "\$$VAR")

        if [ "$VALUE" != "1" ] && [ "$VALUE" != "true" ]; then
            for file in $INI_FILES; do
                if echo "$file" | grep -q "$ext.ini" 2>/dev/null; then
                    rm "$file"
                fi
            done
        fi
    done
fi

APP_USER=$( getent passwd "${APP_UID:-0}" | cut -d : -f 1 )
if [ -z "$APP_USER" ]; then
    if [ -z "$APP_GID" ]; then
       APP_GID="$APP_UID"
    fi

    if [ -f /etc/alpine-release ]; then
        addgroup -g "$APP_GID" app && adduser -D -u "$APP_UID" app -G app
    else
        APP_GROUP=$(getent group "$APP_GID" | cut -d : -f 1)
        if [ -z "$APP_GROUP" ]; then
            groupadd -g "$APP_GID" app
            APP_GROUP=app
        fi
        useradd -u "$APP_UID" app -g "$APP_GROUP"
    fi

    APP_USER=app
fi

# Fix access rights for stdout and stderr
chown "$APP_USER" /proc/self/fd/1 /proc/self/fd/2

if [ ! -f /etc/alpine-release ]; then
    exec runuser -u "$APP_USER" -- /usr/local/bin/docker-php-original-entrypoint "$@"
fi

exec su "$APP_USER" /usr/local/bin/docker-php-original-entrypoint -- "$@"
