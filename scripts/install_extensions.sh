#!/bin/sh
# Package and module lists intentionally use whitespace-separated arguments.
# shellcheck disable=SC2086
set -eu

build_dir=/tmp/php-build
jobs=$(getconf _NPROCESSORS_ONLN)
if [ "$jobs" -gt 8 ]; then jobs=8; fi
export PHP_BUILD_JOBS="$jobs"
config=$(php "$build_dir/build_config.php")
eval "$config"

# Make an accidental use of the legacy clients fail immediately.
rm -f /usr/local/bin/pecl /usr/local/bin/pear /usr/local/bin/peardev

fetch() {
    curl --fail --show-error --location --retry 3 --connect-timeout 15 "$1" -o "$3"
    printf '%s  %s\n' "$2" "$3" | sha256sum -c -
}

has() {
    case " ${EXTENSIONS:-} " in *" $1 "*) return 0 ;; *) return 1 ;; esac
}

build_packages=''
if [ -f /etc/alpine-release ]; then
    distro=alpine
    if [ -n "${EXTENSIONS:-}" ]; then
        build_packages="$PHPIZE_DEPS automake libtool unzip linux-headers pax-utils"
    fi
    has gd && build_packages="$build_packages freetype-dev libjpeg-turbo-dev libpng-dev libxpm-dev libwebp-dev libavif-dev"
    has intl && build_packages="$build_packages icu-dev icu-data-full"
    has ldap && build_packages="$build_packages openldap-dev"
    has zip && build_packages="$build_packages libzip-dev"
    has pdo_pgsql && build_packages="$build_packages postgresql-dev"
    has amqp && build_packages="$build_packages rabbitmq-c-dev"
    has redis && build_packages="$build_packages liblzf-dev zstd-dev lz4-dev"
    if [ -n "${PACKAGES:-}" ]; then apk add --no-cache $PACKAGES; fi
    if [ -n "$build_packages" ]; then apk add --no-cache --virtual .php-extension-build $build_packages; fi
else
    distro=debian
    saved_manual=$(apt-mark showmanual)
    if [ -n "${EXTENSIONS:-}" ]; then
        build_packages="$PHPIZE_DEPS automake libtool unzip"
    fi
    has gd && build_packages="$build_packages libfreetype6-dev libjpeg62-turbo-dev libpng-dev libxpm-dev libwebp-dev libavif-dev"
    has intl && build_packages="$build_packages libicu-dev"
    has ldap && build_packages="$build_packages libldap2-dev"
    has zip && build_packages="$build_packages libzip-dev"
    has pdo_pgsql && build_packages="$build_packages libpq-dev"
    has amqp && build_packages="$build_packages librabbitmq-dev"
    has redis && build_packages="$build_packages liblzf-dev libzstd-dev liblz4-dev"
    if [ -n "$build_packages${PACKAGES:-}" ]; then
        export DEBIAN_FRONTEND=noninteractive
        apt-get update
        apt-get install -y --no-install-recommends $build_packages ${PACKAGES:-}
    fi
fi

if [ -n "$BUILD_CORE" ]; then
    docker-php-source extract
    if has gd; then
        docker-php-ext-configure gd --enable-gd --with-webp --with-jpeg --with-xpm --with-freetype --with-avif
    fi
    if has ldap && [ "$distro" = debian ]; then
        docker-php-ext-configure ldap --with-libdir="lib/$(dpkg-architecture --query DEB_BUILD_MULTIARCH)"
    fi
    docker-php-ext-install -j"$jobs" $BUILD_CORE
fi

if [ -n "$BUILD_EXTERNAL" ]; then
    fetch "$PIE_URL" "$PIE_SHA256" "$build_dir/pie.phar"
    # PIE's temporary home is removed with the sources and registry after building.
    export XDG_CONFIG_HOME="$build_dir/config" XDG_CACHE_HOME="$build_dir/cache"
    if ! sh "$build_dir/install-pie.sh"; then
        find /tmp -maxdepth 1 -type f -name 'pie_*_output_*' -exec cat '{}' ';'
        exit 1
    fi
fi

if [ "$BUILD_XHPROF" = 1 ]; then
    fetch "$XHPROF_URL" "$XHPROF_SHA256" "$build_dir/xhprof.tar.gz"
    mkdir "$build_dir/xhprof"
    tar -xzf "$build_dir/xhprof.tar.gz" --strip-components=1 -C "$build_dir/xhprof"
    (
        cd "$build_dir/xhprof/extension"
        phpize
        ./configure --with-php-config=/usr/local/bin/php-config
        make -j"$jobs"
        make install
    )
    cp -a "$build_dir/xhprof/xhprof_lib" "$build_dir/xhprof/xhprof_html" /usr/local/lib/php/
    docker-php-ext-enable xhprof
fi

if [ -n "${EXTENSIONS:-}" ]; then
    fetch "$COMPOSER_URL" "$COMPOSER_SHA256" /usr/local/bin/composer
    chmod 0755 /usr/local/bin/composer
fi

docker-php-source delete
if [ "$distro" = alpine ] && [ -n "$build_packages" ]; then
    # Keep only shared libraries actually required by the compiled modules.
    needed=$(scanelf --needed --nobanner --format '%n#p' --recursive /usr/local/lib/php/extensions)
    runtime_deps=$(printf '%s\n' "$needed" | tr ',' '\n' | sort -u \
        | awk 'NF && system("test -e /usr/local/lib/" $1) != 0 { print "so:" $1 }')
    apk add --no-cache --virtual .php-extension-runtime $runtime_deps
    if has intl; then apk add --no-cache icu-data-full; fi
    apk del .php-extension-build
elif [ "$distro" = debian ] && [ -n "$build_packages" ]; then
    apt-mark auto '.*' > /dev/null
    apt-mark manual $saved_manual ${PACKAGES:-}
    find /usr/local/lib/php/extensions -name '*.so' -exec ldd '{}' ';' \
        | awk '/=>/ { path=$(NF-1); if (index(path, "/usr/local/") == 1) next; sub("^/(usr/)?", "", path); print "*" path }' | sort -u \
        | xargs -r dpkg-query --search \
        | cut -d: -f1 | sort -u | xargs -r apt-mark manual
    apt-get purge -y --auto-remove -o APT::AutoRemove::RecommendsImportant=false
fi
rm -rf "$build_dir" /root/.cache /root/.config/pie /var/lib/apt/lists/*
