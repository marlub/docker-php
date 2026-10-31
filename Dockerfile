# syntax=docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
# check=skip=InvalidDefaultArgInFrom
ARG PHP_BASE_IMAGE
ARG NODE_BASE_IMAGE="node:24.22.2-alpine3.24@sha256:8a32c8cf649a49915a66e81e7a609223c708d0adabf288ee3f4077d765542f85"
ARG PIE_IMAGE="ghcr.io/php/pie:1.5.5-bin@sha256:3fca03c16df842d02b654dc382db3756058d9c128064f57cace14f8728779ce9"
ARG COMPOSER_IMAGE="ghcr.io/composer/docker:2.11.0-bin@sha256:003932f2656d41d29a5037cb6478f0bba93ed3cd4bd4e86e5ccfed26217044c8"

FROM ${PIE_IMAGE} AS pie
FROM ${COMPOSER_IMAGE} AS composer

FROM ${PHP_BASE_IMAGE} AS runtime
ARG PACKAGES=""
ARG RUNTIME_PACKAGES=""
ARG INSTALL_RUNTIME_PACKAGES=false
COPY build/apk/ /tmp/php-build/apk/
COPY scripts/apk_lock.php /tmp/php-build/apk_lock.php
RUN set -eu; \
    if [ "$INSTALL_RUNTIME_PACKAGES" = true ]; then \
        php /tmp/php-build/apk_lock.php capture /tmp/php-build/baseline.json; \
        apk add --no-cache --virtual .php-installer-tools jq; \
        release=$(cut -d. -f1,2 /etc/alpine-release); \
        case "$(apk --print-arch)" in x86_64) arch=amd64 ;; aarch64) arch=arm64 ;; *) exit 1 ;; esac; \
        lock="/tmp/php-build/apk/$release/$arch.json"; \
        php /tmp/php-build/apk_lock.php check /tmp/php-build/baseline.json "$lock"; \
        jq -er '.packages | to_entries[] | if (.key | test("^[a-zA-Z0-9+_.-]+$")) and (.value | test("^[a-zA-Z0-9+_.~-]+-r[0-9]+$")) then .key + "=" + .value else error("Invalid APK pin") end' "$lock" > /tmp/php-build/pins; \
        set --; while IFS= read -r pin; do set -- "$@" "$pin"; done < /tmp/php-build/pins; \
        apk add --no-cache --virtual .php-extension-runtime $RUNTIME_PACKAGES $PACKAGES "$@"; \
        apk del --no-network .php-installer-tools; \
        php /tmp/php-build/apk_lock.php runtime /tmp/php-build/baseline.json "$lock"; \
    else test "$INSTALL_RUNTIME_PACKAGES" = false; test -z "$PACKAGES"; fi; \
    rm -f /usr/local/bin/pecl /usr/local/bin/pear /usr/local/bin/peardev; \
    rm -rf /tmp/php-build

FROM runtime AS extension-base
ARG BUILD_PACKAGES=""
RUN apk add --no-cache --virtual .php-extension-build $PHPIZE_DEPS $BUILD_PACKAGES

FROM extension-base AS extension-bundled
ARG BUNDLED_EXTENSIONS=""
RUN set -efu; \
    destination="/out$(php-config --extension-dir)"; \
    mkdir -p "$destination" /out/usr/local/etc/php/conf.d; \
    if [ -n "$BUNDLED_EXTENSIONS" ]; then \
        case " $BUNDLED_EXTENSIONS " in *' opcache '*) test "$(php -r 'echo PHP_VERSION_ID;')" -lt 80500 ;; esac; \
        jobs=$(getconf _NPROCESSORS_ONLN); [ "$jobs" -le 8 ] || jobs=8; \
        docker-php-source extract; \
        case " $BUNDLED_EXTENSIONS " in *' gd '*) docker-php-ext-configure gd --enable-gd --with-webp --with-jpeg --with-xpm --with-freetype --with-avif ;; esac; \
        docker-php-ext-install "-j$jobs" $BUNDLED_EXTENSIONS; \
        for extension in $BUNDLED_EXTENSIONS; do \
            cp "$(php-config --extension-dir)/$extension.so" "$destination/"; \
            cp "/usr/local/etc/php/conf.d/docker-php-ext-$extension.ini" /out/usr/local/etc/php/conf.d/; \
        done; \
        docker-php-source delete; \
    fi

FROM extension-base AS extension-redis
ARG BUILD_REDIS=false
ARG REDIS_VERSION=""
ARG REDIS_CONFIGURE_OPTIONS="--enable-redis-lzf --with-liblzf=yes --enable-redis-zstd --with-libzstd=yes --enable-redis-lz4 --with-liblz4=yes"
RUN --mount=type=bind,from=pie,source=/pie,target=/tmp/pie.phar \
    --mount=type=bind,source=scripts/build_pie_extension.sh,target=/tmp/build-pie-extension.sh \
    sh /tmp/build-pie-extension.sh redis phpredis/phpredis "$REDIS_VERSION" "$BUILD_REDIS" $REDIS_CONFIGURE_OPTIONS

FROM extension-base AS extension-amqp
ARG BUILD_AMQP=false
ARG AMQP_VERSION=""
ARG AMQP_CONFIGURE_OPTIONS=""
RUN --mount=type=bind,from=pie,source=/pie,target=/tmp/pie.phar \
    --mount=type=bind,source=scripts/build_pie_extension.sh,target=/tmp/build-pie-extension.sh \
    sh /tmp/build-pie-extension.sh amqp php-amqp/php-amqp "$AMQP_VERSION" "$BUILD_AMQP" $AMQP_CONFIGURE_OPTIONS

FROM extension-base AS extension-xdebug
ARG BUILD_XDEBUG=false
ARG XDEBUG_VERSION=""
ARG XDEBUG_CONFIGURE_OPTIONS=""
RUN --mount=type=bind,from=pie,source=/pie,target=/tmp/pie.phar \
    --mount=type=bind,source=scripts/build_pie_extension.sh,target=/tmp/build-pie-extension.sh \
    sh /tmp/build-pie-extension.sh xdebug xdebug/xdebug "$XDEBUG_VERSION" "$BUILD_XDEBUG" $XDEBUG_CONFIGURE_OPTIONS

FROM extension-base AS extension-pcov
ARG BUILD_PCOV=false
ARG PCOV_VERSION=""
ARG PCOV_CONFIGURE_OPTIONS=""
RUN --mount=type=bind,from=pie,source=/pie,target=/tmp/pie.phar \
    --mount=type=bind,source=scripts/build_pie_extension.sh,target=/tmp/build-pie-extension.sh \
    sh /tmp/build-pie-extension.sh pcov pecl/pcov "$PCOV_VERSION" "$BUILD_PCOV" $PCOV_CONFIGURE_OPTIONS

FROM extension-base AS extension-opentelemetry
ARG BUILD_OPENTELEMETRY=false
ARG OPENTELEMETRY_VERSION=""
ARG OPENTELEMETRY_CONFIGURE_OPTIONS=""
RUN --mount=type=bind,from=pie,source=/pie,target=/tmp/pie.phar \
    --mount=type=bind,source=scripts/build_pie_extension.sh,target=/tmp/build-pie-extension.sh \
    sh /tmp/build-pie-extension.sh opentelemetry open-telemetry/ext-opentelemetry "$OPENTELEMETRY_VERSION" "$BUILD_OPENTELEMETRY" $OPENTELEMETRY_CONFIGURE_OPTIONS

FROM extension-base AS extension-grpc
ARG BUILD_GRPC=false
ARG GRPC_VERSION=""
ARG GRPC_CONFIGURE_OPTIONS=""
RUN --mount=type=bind,from=pie,source=/pie,target=/tmp/pie.phar \
    --mount=type=bind,source=scripts/build_pie_extension.sh,target=/tmp/build-pie-extension.sh \
    sh /tmp/build-pie-extension.sh grpc grpc/grpc-php-ext "$GRPC_VERSION" "$BUILD_GRPC" $GRPC_CONFIGURE_OPTIONS

FROM extension-base AS extension-protobuf
ARG BUILD_PROTOBUF=false
ARG PROTOBUF_VERSION=""
ARG PROTOBUF_CONFIGURE_OPTIONS=""
RUN --mount=type=bind,from=pie,source=/pie,target=/tmp/pie.phar \
    --mount=type=bind,source=scripts/build_pie_extension.sh,target=/tmp/build-pie-extension.sh \
    sh /tmp/build-pie-extension.sh protobuf extport/protobuf "$PROTOBUF_VERSION" "$BUILD_PROTOBUF" $PROTOBUF_CONFIGURE_OPTIONS

FROM extension-base AS extension-xhprof
ARG BUILD_XHPROF=false
ARG XHPROF_TAG=""
ARG XHPROF_COMMIT=""
ARG XHPROF_CONFIGURE_OPTIONS=""
RUN set -efu; \
    destination="/out$(php-config --extension-dir)"; \
    mkdir -p "$destination" /out/usr/local/etc/php/conf.d; \
    if [ "$BUILD_XHPROF" = true ]; then \
        printf '%s\n' "$XHPROF_COMMIT" | grep -Eq '^[a-f0-9]{40}$'; \
        printf '%s\n' "$XHPROF_TAG" | grep -Eq '^v?[0-9]+\.[0-9]+\.[0-9]+$'; \
        git init /tmp/xhprof; \
        git -C /tmp/xhprof remote add origin https://github.com/longxinH/xhprof.git; \
        git -C /tmp/xhprof fetch --depth=1 origin "$XHPROF_COMMIT"; \
        git -C /tmp/xhprof checkout --detach FETCH_HEAD; \
        test "$(git -C /tmp/xhprof rev-parse HEAD)" = "$XHPROF_COMMIT"; \
        cd /tmp/xhprof/extension; \
        version=$(sed -n 's/^#define[[:space:]][[:space:]]*XHPROF_VERSION[[:space:]][[:space:]]*"\([^"]*\)".*/\1/p' php_xhprof.h); \
        printf '%s\n' "$version" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; \
        jq -n --arg tag "$XHPROF_TAG" --arg commit "$XHPROF_COMMIT" --arg version "$version" \
            '{tag: $tag, commit: $commit, version: $version}' > /out/usr/local/lib/php/xhprof-build.json; \
        phpize; ./configure --with-php-config=/usr/local/bin/php-config $XHPROF_CONFIGURE_OPTIONS; \
        jobs=$(getconf _NPROCESSORS_ONLN); [ "$jobs" -le 8 ] || jobs=8; \
        make "-j$jobs"; make install; \
        strip --strip-unneeded "$(php-config --extension-dir)/xhprof.so"; \
        docker-php-ext-enable xhprof; \
        cp "$(php-config --extension-dir)/xhprof.so" "$destination/"; \
        cp /usr/local/etc/php/conf.d/docker-php-ext-xhprof.ini /out/usr/local/etc/php/conf.d/; \
        cp -a /tmp/xhprof/xhprof_lib /tmp/xhprof/xhprof_html /out/usr/local/lib/php/; \
        cd /; rm -rf /tmp/xhprof; \
    else test "$BUILD_XHPROF" = false; fi

FROM runtime AS php
ARG EXTENSIONS=""
ENV EXTENSIONS="${EXTENSIONS}"
COPY --from=extension-bundled /out/ /
COPY --from=extension-redis /out/ /
COPY --from=extension-amqp /out/ /
COPY --from=extension-xdebug /out/ /
COPY --from=extension-pcov /out/ /
COPY --from=extension-opentelemetry /out/ /
COPY --from=extension-grpc /out/ /
COPY --from=extension-protobuf /out/ /
COPY --from=extension-xhprof /out/ /

RUN --mount=type=bind,from=composer,source=/composer,target=/tmp/composer \
    if [ -n "$EXTENSIONS" ]; then cp /tmp/composer /usr/local/bin/composer; chmod 0755 /usr/local/bin/composer; fi
RUN cp /usr/local/etc/php/php.ini-production /usr/local/etc/php/php.ini
RUN mv /usr/local/bin/docker-php-entrypoint /usr/local/bin/docker-php-original-entrypoint
COPY --chmod=0755 entrypoint.sh /usr/local/bin/docker-php-entrypoint

FROM ${NODE_BASE_IMAGE:-node:24-alpine} AS node

FROM php AS php-node

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/include/node /usr/local/include/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=node /usr/lib /usr/lib

RUN ln -vs /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm
