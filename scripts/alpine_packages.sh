#!/bin/sh
# Shared profiles for extension compilation and runtime-lock refreshes.
case "${1:-build}" in
    build) printf '%s\n' "$PHPIZE_DEPS automake libtool unzip linux-headers git freetype-dev libjpeg-turbo-dev libpng-dev libxpm-dev libwebp-dev libavif-dev icu-dev openldap-dev libzip-dev postgresql-dev rabbitmq-c-dev liblzf-dev zstd-dev lz4-dev" ;;
    runtime) printf '%s\n' 'brotli-libs freetype libjpeg-turbo libpng libxpm libwebp libavif icu-libs icu-data-full libldap libzip libpq rabbitmq-c liblzf zstd-libs lz4-libs libstdc++ libgcc' ;;
    *) echo 'Expected build or runtime package profile' >&2; exit 1 ;;
esac
