# docker-php ⚠️ BETA - breaking changes possible
Adding common PHP extensions to some of the official PHP Docker images.

## 🐳 Image variants

All minor versions and the latest patch of each minor image tag are daily updated. EOL versions are not supported or updated at all. The default php.ini is based on php.ini-production

| ⚙️ PHP version | ✨ Image variants |
| -------------- | ------------------ |
| 8.5 | ghcr.io/marlub/php:8.5-fpm-alpine<br>ghcr.io/marlub/php:8.5-fpm-alpine-node24 |
| 8.4 | ghcr.io/marlub/php:8.4-fpm-alpine<br>ghcr.io/marlub/php:8.4-fpm-alpine-node24 |
| 8.3 | ghcr.io/marlub/php:8.3-fpm-alpine<br>ghcr.io/marlub/php:8.3-fpm-alpine-node24 |
| 8.2 | ghcr.io/marlub/php:8.2-fpm-alpine<br>ghcr.io/marlub/php:8.2-fpm-alpine-node24 |
| 8.1 | ghcr.io/marlub/php:8.1-fpm-alpine<br>ghcr.io/marlub/php:8.1-fpm-alpine-node24 |

## 🧬 Extensions added on top of the official PHP images
```
bcmath gd exif intl calendar ldap zip pcntl opcache sockets mysqli pdo_pgsql pdo_mysql redis amqp xdebug pcov xhprof
```
All extensions are disabled by default, so new extensions can be added anytime without the risk of breaking setups. You can enable extensions with environment variables prefixed with `PHP_EXTENSION_[NAME]`, for example `PHP_EXTENSION_INTL=true`.

### Customizations
- With PHP 8.5 opcache is shipped by the official image and no longer managed by this one.
- amqp is compiled from source for PHP 8.5 until a pecl version is released.

### All extension environment variables
```
PHP_EXTENSION_BCMATH="true"
PHP_EXTENSION_GD="true"
PHP_EXTENSION_EXIF="true"
PHP_EXTENSION_INTL="true"
PHP_EXTENSION_CALENDAR="true"
PHP_EXTENSION_LDAP="true"
PHP_EXTENSION_ZIP="true"
PHP_EXTENSION_PCNTL="true"
PHP_EXTENSION_OPCACHE="true"
PHP_EXTENSION_SOCKETS="true"
PHP_EXTENSION_MYSQLI="true"
PHP_EXTENSION_PDO_PGSQL="true"
PHP_EXTENSION_PDO_MYSQL="true"
PHP_EXTENSION_REDIS="true"
PHP_EXTENSION_AMQP="true"
PHP_EXTENSION_XDEBUG="false"
PHP_EXTENSION_PCOV="false"
PHP_EXTENSION_XHPROF="false"
```
#### It's highly recommended to enable only required extensions

## 🔐 Container user
The default is inherited from the official PHP image. The FPM daemon runs as `root` and the worker processes run as `www-data`. However you can change the user by setting the `APP_UID` and `APP_GID` environment variables. If the user id does not exists a user named `app` will be created.

Do not change the user or group id via docker cli argument `--user`. Root is required to enable/disable the php extensions based on your environment variables.

```
APP_UID=1000
APP_GID=1000
```

## 🧩 Node.js
Most image tags provide further variations with a node version included. Such tags end with `node[version]`.
