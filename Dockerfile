ARG BASE_TAG="8.4-fpm-alpine"
ARG NODE_VERSION=""
ARG OS="alpine"
ARG PHP_BASE_IMAGE="php:${BASE_TAG}"
ARG NODE_BASE_IMAGE="node:${NODE_VERSION:-24}-${OS:-slim}"

FROM ${PHP_BASE_IMAGE} AS php

ARG EXTENSIONS=""
ARG PECL=""
ARG PACKAGES=""
ARG EXTENSIONS_INSTALL=""
ARG PECL_INSTALL=""
ARG COMPOSER_VERSION=""
ARG INSTALLER_URL="https://github.com/mlocati/docker-php-extension-installer/releases/latest/download/install-php-extensions"
ARG INSTALLER_SHA256=""

ENV EXTENSIONS="${EXTENSIONS}"
ENV PECL="${PECL}"

ADD --chmod=0755 ${INSTALLER_URL} /usr/local/bin/install-php-extensions

RUN set -eux; \
    if [ -n "$INSTALLER_SHA256" ]; then \
        printf '%s  %s\n' "$INSTALLER_SHA256" /usr/local/bin/install-php-extensions | sha256sum -c -; \
    fi; \
    echo "Core extensions:  $EXTENSIONS"; \
    echo "PECL extensions:  $PECL"; \
    echo "System packages:  $PACKAGES"; \
    \
    #========== System package installation ==========#
    if [ -n "$PACKAGES" ]; then \
        if [ -f /etc/alpine-release ]; then \
            apk add --no-cache $PHPIZE_DEPS $PACKAGES; \
        else \
            apt-get update; \
            apt-get install -y --no-install-recommends $PHPIZE_DEPS $PACKAGES; \
            rm -rf /var/lib/apt/lists/*; \
        fi; \
    fi; \
    \
    #========== PHP extensions ==========#
    if [ -n "$EXTENSIONS" ]; then \
        install-php-extensions "@composer${COMPOSER_VERSION:+-$COMPOSER_VERSION}" ${EXTENSIONS_INSTALL:-$EXTENSIONS}; \
    fi; \
    \
    #========== PECL extensions ==========#
    if [ -n "$PECL" ]; then \
        pecl install --onlyreqdeps --force ${PECL_INSTALL:-$PECL}; \
        docker-php-ext-enable $PECL; \
    fi;

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
