#!/bin/sh
# One PIE compiler stage: explicit inputs in, runtime artifacts out.
set -efu

extension=$1
package=$2
version=$3
enabled=$4
shift 4

DESTDIR=${DESTDIR:-/out}
destination="$DESTDIR$(php-config --extension-dir)"
mkdir -p "$destination" "$DESTDIR/usr/local/etc/php/conf.d"
case "$enabled" in
    false) exit 0 ;;
    true) ;;
    *) echo 'Expected an explicit extension build switch' >&2; exit 1 ;;
esac
printf '%s\n' "$extension" | grep -Eq '^[a-z][a-z0-9_]*$'
printf '%s\n' "$package" | grep -Eq '^[a-z0-9_.-]+/[a-z0-9_.-]+$'
printf '%s\n' "$version" | grep -Eq '^[0-9]+(\.[0-9]+){1,3}$'
for option in "$@"; do
    printf '%s\n' "$option" | grep -Eq '^--[a-z0-9-]+(=[a-z0-9/.+-]+)?$'
done

jobs=$(getconf _NPROCESSORS_ONLN)
[ "$jobs" -le 8 ] || jobs=8
export XDG_CONFIG_HOME=/tmp/pie-config XDG_CACHE_HOME=/tmp/pie-cache
if php /tmp/pie.phar --no-interaction --no-ansi install --no-cache \
    --skip-enable-extension "--make-parallel-jobs=$jobs" "$package:$version" "$@"; then
    module="$(php-config --extension-dir)/$extension.so"
    # Strip before copying so the compiler cache and runtime both stay small.
    strip --strip-unneeded "$module"
    docker-php-ext-enable "$extension"
else
    set +f
    for log in /tmp/pie_*_output_*; do
        [ ! -f "$log" ] || cat "$log" >&2
    done
    exit 1
fi
cp "$module" "$destination/"
cp "${PHP_INI_DIR:-/usr/local/etc/php}/conf.d/docker-php-ext-$extension.ini" "$DESTDIR/usr/local/etc/php/conf.d/"
rm -rf /tmp/pie-config /tmp/pie-cache /root/.cache /root/.config/pie
