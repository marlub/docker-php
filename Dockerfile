ARG BASE_TAG="8.4-fpm-alpine"
ARG NODE_VERSION=""
ARG PHP_BASE_IMAGE="php:${BASE_TAG}"
ARG NODE_BASE_IMAGE="node:24.22.1-alpine3.24@sha256:06d7adf3e0ef61a13f1d28682726366f97a15aa1888c934d3ecec86c33918b53"
ARG PIE_IMAGE="ghcr.io/php/pie:1.5.3-bin@sha256:9d3e6d9dafa5ae8cf034489260e206a5aa1b488b29c0ddaea9149826d9e32f52"
ARG COMPOSER_IMAGE="ghcr.io/composer/docker:2.10.3-bin@sha256:016c41066558559142758092f7ef286d66ae113df23d49e086fdda0166dd5f01"

FROM ${PIE_IMAGE} AS pie
FROM ${COMPOSER_IMAGE} AS composer

FROM ${PHP_BASE_IMAGE} AS php

ARG EXTENSIONS=""
ARG PACKAGES=""

ENV EXTENSIONS="${EXTENSIONS}"

COPY build/ /tmp/php-build/build/
COPY scripts/install_extensions.sh scripts/apk_lock.php scripts/alpine_packages.sh /tmp/php-build/
RUN --mount=type=bind,from=pie,source=/pie,target=/tmp/pie.phar \
    --mount=type=bind,from=composer,source=/composer,target=/tmp/composer \
    sh /tmp/php-build/install_extensions.sh

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
