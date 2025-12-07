ARG BASE_TAG="8.4-fpm-alpine"
ARG NODE_VERSION=""
ARG OS="alpine"

FROM php:${BASE_TAG} AS php

ARG EXTENSIONS=""
ARG PECL=""
ARG PACKAGES=""

ENV EXTENSIONS="${EXTENSIONS}"
ENV PECL="${PECL}"

ADD --chmod=0755 https://github.com/mlocati/docker-php-extension-installer/releases/latest/download/install-php-extensions /usr/local/bin/

RUN set -eux; \
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
        install-php-extensions $EXTENSIONS; \
    fi; \
    \
    #========== PECL extensions ==========#
    if [ -n "$PECL" ]; then \
        pecl install --onlyreqdeps --force $PECL; \
        docker-php-ext-enable $PECL; \
    fi;

RUN mv /usr/local/bin/docker-php-entrypoint /usr/local/bin/docker-php-original-entrypoint
COPY --chmod=0755 entrypoint.sh /usr/local/bin/docker-php-entrypoint

FROM node:${NODE_VERSION:-24}-${OS:-slim} AS node

FROM php AS php-node

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/include/node /usr/local/include/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=node /usr/lib /usr/lib

RUN ln -vs /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm
