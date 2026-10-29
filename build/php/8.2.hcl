variable "PHP_8_2" {
  type = map(string)
  default = {
    PHP_BASE_IMAGE = "php:8.2.35-fpm-alpine3.24@sha256:f7d0ea0acdaa28bef474b3741ef0ee00423f05078e216d31f1aa5c4a681ae6d4"
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
    GRPC_VERSION = "1.85.0"
    # renovate: datasource=packagist depName=extport/protobuf
    PROTOBUF_VERSION = "36.2.0.1"
    PROTOBUF_MODULE_VERSION = "5.36.2"
    # renovate: datasource=github-tags depName=longxinH/xhprof
    XHPROF_TAG = "v2.3.10"
    XHPROF_COMMIT = "da774867b337de4a1caeb5dfdd6a3f6793f73391"
  }
}

group "ci-8-2" {
  targets = ["php-8-2", "php-8-2-node24"]
}

target "_php-8-2" {
  labels = { "io.github.marlub.docker-php.base-target" = "php-8-2" }
  inherits = ["_common"]
  args = extension_args(PHP_8_2)
}

target "php-8-2" {
  inherits = ["_php-8-2"]
  target = "php"
  tags = ["docker-php:8.2-fpm-alpine"]
}

target "php-8-2-node24" {
  inherits = ["_php-8-2"]
  target = "php-node"
  tags = ["docker-php:8.2-fpm-alpine-node24"]
}
