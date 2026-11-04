variable "PHP_8_4" {
  type = map(string)
  default = {
    PHP_BASE_IMAGE = "php:8.4.27-fpm-alpine3.24@sha256:93aa3f61216b73cc6f878235c9772849449f4efc18dfe3d6678eed645076c00d"
    EXTENSIONS = "bcmath gd exif intl calendar ldap zip pcntl opcache sockets mysqli pdo_pgsql pdo_mysql redis amqp xdebug pcov xhprof opentelemetry grpc protobuf"
    PACKAGES = ""
    # renovate: datasource=packagist depName=phpredis/phpredis
    REDIS_VERSION = "6.3.0"
    # renovate: datasource=packagist depName=php-amqp/php-amqp
    AMQP_VERSION = "2.2.0"
    # renovate: datasource=packagist depName=xdebug/xdebug
    XDEBUG_VERSION = "3.5.3"
    # renovate: datasource=packagist depName=pecl/pcov
    PCOV_VERSION = "1.0.12"
    # renovate: datasource=packagist depName=open-telemetry/ext-opentelemetry
    OPENTELEMETRY_VERSION = "1.4.2"
    # renovate: datasource=packagist depName=grpc/grpc-php-ext
    GRPC_VERSION = "1.85.1"
    # renovate: datasource=packagist depName=extport/protobuf
    PROTOBUF_VERSION = "36.2.0.1"
    PROTOBUF_MODULE_VERSION = "5.36.2"
    # renovate: datasource=github-tags depName=longxinH/xhprof
    XHPROF_TAG = "v2.3.10"
    XHPROF_COMMIT = "da774867b337de4a1caeb5dfdd6a3f6793f73391"
  }
}

group "ci-8-4" {
  targets = ["php-8-4-alpine", "php-8-4-alpine-node24", "php-8-4-trixie", "php-8-4-trixie-node24"]
}

target "_php-8-4-alpine" {
  labels = { "io.github.marlub.docker-php.base-target" = "php-8-4-alpine" }
  inherits = ["_common"]
  args = extension_args(PHP_8_4)
}

target "php-8-4-alpine" {
  inherits = ["_php-8-4-alpine"]
  target = "php"
  tags = ["docker-php:8.4-fpm-alpine3.24"]
}

target "php-8-4-alpine-node24" {
  inherits = ["_php-8-4-alpine"]
  target = "php-node"
  tags = ["docker-php:8.4-fpm-alpine3.24-node24"]
}

target "_php-8-4-trixie" {
  inherits = ["_common", "_debian"]
  labels = { "io.github.marlub.docker-php.base-target" = "php-8-4-trixie", "io.github.marlub.docker-php.default-debian" = "true" }
  args = extension_args(merge(PHP_8_4, {
    PHP_BASE_IMAGE = "php:8.4.27-fpm-trixie@sha256:3a6527017daca7917709afa08a37388ec108db093857f31d642cfff2e6f0449e"
  }))
}

target "php-8-4-trixie" {
  inherits = ["_php-8-4-trixie"]
  target = "php"
  tags = ["docker-php:8.4-fpm-trixie"]
}

target "php-8-4-trixie-node24" {
  inherits = ["_php-8-4-trixie"]
  target = "php-node"
  tags = ["docker-php:8.4-fpm-trixie-node24"]
}
