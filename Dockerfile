ARG BASE_TAG="8.4-fpm-alpine"
ARG NODE_VERSION=""
ARG OS="alpine"
ARG PHP_BASE_IMAGE="php:${BASE_TAG}"
ARG NODE_BASE_IMAGE="node:${NODE_VERSION:-24}-${OS:-slim}"

FROM ${PHP_BASE_IMAGE} AS php

ARG EXTENSIONS=""
ARG PACKAGES=""
ARG DEPENDENCIES=""

ENV EXTENSIONS="${EXTENSIONS}"

COPY scripts/install_extensions.sh scripts/build_config.php scripts/build-defaults.json /tmp/php-build/
RUN sh /tmp/php-build/install_extensions.sh

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
