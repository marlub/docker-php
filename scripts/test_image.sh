#!/usr/bin/env sh
# Lists intentionally split into arguments; PHP and nested shell code stay literal.
# shellcheck disable=SC2016,SC2086
set -eu

IMAGE=''
NODE_VERSION=''
EXTENSIONS=''
for argument in "$@"; do
    case "$argument" in
        --image=*) IMAGE=${argument#*=} ;;
        --node=*) NODE_VERSION=${argument#*=} ;;
        --extensions=*) EXTENSIONS=${argument#*=} ;;
        *) echo "Invalid argument: $argument" >&2; exit 1 ;;
    esac
done
[ -n "$IMAGE" ]
IMAGE_PLATFORM=$(docker image inspect "$IMAGE" --format '{{.Os}}/{{.Architecture}}')

# PHP startup diagnostics can be printed on stderr while returning exit code 0.
run_container() {
    result=$(docker run --rm --platform "$IMAGE_PLATFORM" "$@" 2>&1) || { echo "$result" >&2; return 1; }
    if printf '%s\n' "$result" | grep -Ei 'PHP (Warning|Fatal|Parse|Startup)|Unable to load dynamic library'; then
        echo "Unexpected PHP diagnostic" >&2
        return 1
    fi
    printf '%s\n' "$result"
}

check_modules='foreach (array_slice($argv, 1) as $name) {
    $loaded = extension_loaded($name === "opcache" ? "Zend OPcache" : $name);
    echo $name, "=", $loaded ? "1" : "0", "\n";
}'

echo '> All managed extensions are disabled by default'
# Extension names come from the validated build matrix.
output=$(run_container "$IMAGE" php -r "$check_modules" -- $EXTENSIONS)
if printf '%s\n' "$output" | grep -q '=1$'; then
    echo "$output" >&2
    exit 1
fi

echo '> Each extension can be enabled independently'
for extension in $EXTENSIONS; do
    variable=$(printf '%s' "$extension" | tr '[:lower:]' '[:upper:]')
    output=$(run_container -e "PHP_EXTENSION_$variable=true" "$IMAGE" php -r "$check_modules" -- $EXTENSIONS)
    printf '%s\n' "$output" | grep -qx "$extension=1"
    if printf '%s\n' "$output" | grep '=1$' | grep -vx "$extension=1"; then
        echo 'Enabling one extension unexpectedly enabled another' >&2
        exit 1
    fi
    output=$(run_container -e "PHP_EXTENSION_$variable=1" "$IMAGE" php -r "$check_modules" -- "$extension")
    [ "$output" = "$extension=1" ]
done

echo '> All extensions can be loaded together'
set --
for extension in $EXTENSIONS; do
    variable=$(printf '%s' "$extension" | tr '[:lower:]' '[:upper:]')
    set -- "$@" -e "PHP_EXTENSION_$variable=true"
done
output=$(run_container "$@" "$IMAGE" php -r "$check_modules" -- $EXTENSIONS)
if printf '%s\n' "$output" | grep -q '=0$'; then
    echo "$output" >&2
    exit 1
fi

case " $EXTENSIONS " in
    *' gd '*)
        echo '> GD retains JPEG, PNG, WebP, XPM, FreeType, and AVIF support'
        run_container -e PHP_EXTENSION_GD=true "$IMAGE" php -r '
            $info = gd_info();
            foreach (["JPEG Support", "PNG Support", "WebP Support", "XPM Support", "FreeType Support", "AVIF Support"] as $feature) {
                if (empty($info[$feature])) { fwrite(STDERR, "Missing GD feature: $feature\n"); exit(1); }
            }
        '
        ;;
esac
case " $EXTENSIONS " in
    *' redis '*)
        echo '> Redis compression round trips work without a server'
        run_container -e PHP_EXTENSION_REDIS=true "$IMAGE" php -r '
            $redis = new Redis();
            $value = str_repeat("compression test ", 256);
            foreach ([Redis::COMPRESSION_LZF, Redis::COMPRESSION_ZSTD, Redis::COMPRESSION_LZ4] as $compression) {
                $redis->setOption(Redis::OPT_COMPRESSION, $compression);
                if ($redis->_uncompress($redis->_compress($value)) !== $value) { exit(1); }
            }
        '
        ;;
esac
case " $EXTENSIONS " in
    *' xhprof '*)
        echo '> XHProf can collect a profile and retains its PHP support files'
        run_container -e PHP_EXTENSION_XHPROF=true "$IMAGE" php -r '
            xhprof_enable(); strlen(str_repeat("x", 10)); $profile = xhprof_disable();
            if (!$profile || !is_file("/usr/local/lib/php/xhprof_lib/utils/xhprof_lib.php") ||
                !is_file("/usr/local/lib/php/xhprof_html/index.php")) { exit(1); }
        '
        ;;
esac

if [ -n "$NODE_VERSION" ]; then
    echo "> Node $NODE_VERSION and npm work"
    output=$(run_container "$IMAGE" node -v)
    case "$output" in "v$NODE_VERSION."*) ;; *) echo "$output" >&2; exit 1 ;; esac
    run_container "$IMAGE" npm --version
fi

echo '> PIE and legacy installer clients are absent'
run_container "$IMAGE" sh -c 'for tool in pie pecl pear peardev install-php-extensions; do if command -v "$tool"; then exit 1; fi; done'

echo '> Custom UID and FPM configuration work'
output=$(run_container -e APP_UID=1000 "$IMAGE" sh -c 'id -u; php-fpm -t >/dev/null 2>&1')
[ "$output" = 1000 ]
echo '> Default UID and FPM configuration work'
output=$(run_container "$IMAGE" sh -c 'id -u; php-fpm -t >/dev/null 2>&1')
[ "$output" = 0 ]
