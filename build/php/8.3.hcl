variable "PHP_8_3" {
  type = map(string)
  default = {
    PHP_BASE_IMAGE = "php:8.3.36-fpm-alpine3.24@sha256:25e15cf35ed0f5b75d8cf14fc2c4ed7e0e6bee00d359e681b7fa3e91a7bee49f"
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

group "ci-8-3" {
  targets = ["php-8-3", "php-8-3-node24"]
}

target "_php-8-3" {
  labels = { "io.github.marlub.docker-php.base-target" = "php-8-3" }
  inherits = ["_common"]
  args = extension_args(PHP_8_3)
}

target "php-8-3" {
  inherits = ["_php-8-3"]
  target = "php"
  tags = ["docker-php:8.3-fpm-alpine"]
}

target "php-8-3-node24" {
  inherits = ["_php-8-3"]
  target = "php-node"
  tags = ["docker-php:8.3-fpm-alpine-node24"]
}
