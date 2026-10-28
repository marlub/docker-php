#!/bin/sh
# Exercise the PIE helper with explicit pins and isolated command/artifact stubs.
set -eu
root=$(CDPATH='' cd -P "$(dirname "$0")/.." && pwd)
temporary=$(mktemp -d)
trap 'rm -rf "$temporary"' EXIT HUP INT TERM
export ARGUMENT_LOG="$temporary/arguments"
export STRIP_LOG="$temporary/strip-arguments"
export PHP_INI_DIR="$temporary/php"
export EXTENSION_DIRECTORY="$temporary/extensions"
export DESTDIR="$temporary/out"
mkdir -p "$temporary/bin" "$EXTENSION_DIRECTORY" "$PHP_INI_DIR/conf.d"
printf 'module\n' > "$EXTENSION_DIRECTORY/redis.so"
printf 'extension=redis.so\n' > "$PHP_INI_DIR/conf.d/docker-php-ext-redis.ini"
cat > "$temporary/bin/php" <<'STUB'
#!/bin/sh
printf '%s\n' "$@" > "$ARGUMENT_LOG"
exit "${COMMAND_STATUS:-0}"
STUB
cat > "$temporary/bin/php-config" <<'STUB'
#!/bin/sh
printf '%s\n' "$EXTENSION_DIRECTORY"
STUB
printf '#!/bin/sh\nexit 0\n' > "$temporary/bin/docker-php-ext-enable"
cat > "$temporary/bin/strip" <<'STUB'
#!/bin/sh
printf '%s\n' "$@" > "$STRIP_LOG"
[ "${STRIP_STATUS:-0}" -eq 0 ] || exit "$STRIP_STATUS"
printf 'stripped module\n' > "$2"
STUB
chmod +x "$temporary/bin/php" "$temporary/bin/php-config" "$temporary/bin/docker-php-ext-enable" "$temporary/bin/strip"
export PATH="$temporary/bin:$PATH"

rejects() {
    if "$@" > /dev/null 2>&1; then
        echo "Expected PIE helper rejection: $*" >&2
        exit 1
    fi
}

sh "$root/scripts/build_pie_extension.sh" redis phpredis/phpredis 6.3.0 true --enable-redis-lzf --with-liblzf=yes
grep -qx 'phpredis/phpredis:6.3.0' "$ARGUMENT_LOG"
grep -qx -- '--enable-redis-lzf' "$ARGUMENT_LOG"
grep -qx -- '--with-liblzf=yes' "$ARGUMENT_LOG"
grep -qx -- '--strip-unneeded' "$STRIP_LOG"
grep -qx -- "$EXTENSION_DIRECTORY/redis.so" "$STRIP_LOG"
grep -qx 'stripped module' "$DESTDIR$EXTENSION_DIRECTORY/redis.so"
cmp "$EXTENSION_DIRECTORY/redis.so" "$DESTDIR$EXTENSION_DIRECTORY/redis.so"
cmp "$PHP_INI_DIR/conf.d/docker-php-ext-redis.ini" "$DESTDIR/usr/local/etc/php/conf.d/docker-php-ext-redis.ini"

# Disabled extensions need neither a pin nor any installation command.
rm "$ARGUMENT_LOG" "$STRIP_LOG"
sh "$root/scripts/build_pie_extension.sh" grpc grpc/grpc-php-ext '' false
test ! -e "$ARGUMENT_LOG"
test ! -e "$STRIP_LOG"

# Package versions and options are literal validated arguments, never evaluated.
for version in '' latest '6.3.0;command'; do
    rejects sh "$root/scripts/build_pie_extension.sh" redis phpredis/phpredis "$version" true
done
rejects sh "$root/scripts/build_pie_extension.sh" 'redis;command' phpredis/phpredis 6.3.0 true
rejects sh "$root/scripts/build_pie_extension.sh" redis bad-package 6.3.0 true
rejects sh "$root/scripts/build_pie_extension.sh" redis phpredis/phpredis 6.3.0 true '--flag;command'
rejects sh "$root/scripts/build_pie_extension.sh" redis phpredis/phpredis 6.3.0 maybe
export COMMAND_STATUS=17
rejects sh "$root/scripts/build_pie_extension.sh" redis phpredis/phpredis 6.3.0 true
export COMMAND_STATUS=0 STRIP_STATUS=18
rejects sh "$root/scripts/build_pie_extension.sh" redis phpredis/phpredis 6.3.0 true
echo 'PIE pin, argument, selection, stripped artifact, and failure checks passed'
