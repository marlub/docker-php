#!/usr/bin/env sh
set -e

if [ ! -f /tmp/.extensions-disabled ]; then
    touch /tmp/.extensions-disabled

    INI_FILES=$(php --ini | awk '/\.ini/ {print $NF}' | tr -d ',' | sort -u)

    for ext in $EXTENSIONS $PECL; do
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

exec /usr/local/bin/docker-php-original-entrypoint "$@"
