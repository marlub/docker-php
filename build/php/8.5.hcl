variable "PHP_8_5" {
  type = map(string)
  default = {
    PHP_BASE_IMAGE = "php:8.5.12-fpm-alpine3.24@sha256:4c2818074ec49f6abe46620f3965e0cb75bc3a5f2b417edd1aacaba6f091c40e"
    EXTENSIONS = "bcmath gd exif intl calendar ldap zip pcntl sockets mysqli pdo_pgsql pdo_mysql redis amqp xdebug pcov xhprof opentelemetry grpc protobuf"
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

group "ci-8-5" {
  targets = ["php-8-5-alpine", "php-8-5-alpine-node24", "php-8-5-trixie", "php-8-5-trixie-node24"]
}

target "_php-8-5-alpine" {
  labels = { "io.github.marlub.docker-php.base-target" = "php-8-5-alpine" }
  inherits = ["_common"]
  args = extension_args(PHP_8_5)
}

target "php-8-5-alpine" {
  inherits = ["_php-8-5-alpine"]
  target = "php"
  tags = ["docker-php:8.5-fpm-alpine"]
}

target "php-8-5-alpine-node24" {
  inherits = ["_php-8-5-alpine"]
  target = "php-node"
  tags = ["docker-php:8.5-fpm-alpine-node24"]
}

target "_php-8-5-trixie" {
  inherits = ["_common", "_debian"]
  labels = { "io.github.marlub.docker-php.base-target" = "php-8-5-trixie" }
  args = extension_args(merge(PHP_8_5, {
    PHP_BASE_IMAGE = "php:8.5.12-fpm-trixie@sha256:da5d8e27ec238adc0804fd4194f98e59477db7116690d275b76679c1b84c813f"
  }))
}

target "php-8-5-trixie" {
  inherits = ["_php-8-5-trixie"]
  target = "php"
  tags = ["docker-php:8.5-fpm-trixie"]
}

target "php-8-5-trixie-node24" {
  inherits = ["_php-8-5-trixie"]
  target = "php-node"
  tags = ["docker-php:8.5-fpm-trixie-node24"]
}
