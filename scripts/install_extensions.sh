#!/bin/sh
# Lists from validated configuration and public build arguments intentionally split.
# shellcheck disable=SC2086
set -efu

fail() {
    echo "$*" >&2
    exit 1
}

extension_plan() {
    jq -es --arg extensions "$3" --argjson php "$2" '
        if length != 1 or (.[0] | type) != "object" then
            error("Expected one PHP manifest object")
        else .[0] end |
        . as $config |
        ($extensions | [splits("\\s+") | select(length > 0)]) as $selected |
        if all($selected[]; test("^[a-z][a-z0-9_]*\\z")) | not then
            error("Invalid extension name")
        elif $php >= 80500 and ($selected | index("opcache")) != null then
            error("PHP 8.5+ opcache is built in")
        else . end |
        (reduce $selected[] as $name ([];
            if $config.pie_packages[$name] == null or index($name) != null then .
            else . + [$name] end)) as $external |
        if all($external[]; $config.pie_packages[.] |
            (.package | test("^[a-z0-9_.-]+/[a-z0-9_.-]+\\z")) and
            (.version | test("^[0-9]+(\\.[0-9]+){1,3}\\z")) and
            ((has("module_version") | not) or
                (.module_version | type == "string" and test("^[0-9]+(\\.[0-9]+){1,3}\\z"))) and
            (.configure_options | type == "array" and all(.[];
                test("^--[a-z0-9-]+(=[a-z0-9/.+-]+)?\\z")))) | not then
            error("Invalid PIE pin or configure option")
        elif ($selected | index("xhprof")) != null and
            (($config.xhprof.commit | test("^[a-f0-9]{40}\\z")) and
             ($config.xhprof.tag | test("^v?[0-9]+\\.[0-9]+\\.[0-9]+\\z")) | not) then
            error("Invalid XHProf source pin")
        else {
            core: [$selected[] | . as $name | select($name != "xhprof" and ($external | index($name)) == null)],
            external: $external,
            xhprof: (($selected | index("xhprof")) != null)
        } end
    ' "$1"
}

read_configuration() {
    [ -f /etc/alpine-release ] || fail 'Only Alpine PHP base images are supported.'
    directory=$(CDPATH='' cd -P "$(dirname "$0")" && pwd)
    minor=$(php -r 'echo PHP_MAJOR_VERSION, ".", PHP_MINOR_VERSION;')
    php_version=$(php -r 'echo PHP_VERSION_ID;')
    config="$directory/build/php/$minor.json"
    baseline="$directory/baseline.json"
    release=$(cut -d. -f1,2 /etc/alpine-release)
    architecture=$(apk --print-arch)
    jobs=$(getconf _NPROCESSORS_ONLN)
    [ "$jobs" -ge 1 ] || jobs=1
    [ "$jobs" -le 8 ] || jobs=8
    EXTENSIONS=${EXTENSIONS:-}
    PACKAGES=${PACKAGES:-}

    php "$directory/apk_lock.php" capture "$baseline"
    apk add --no-cache --virtual .php-installer-tools jq

    extension_plan "$config" "$php_version" "$EXTENSIONS" > "$directory/selection.json"
    core=$(jq -r '.core | join(" ")' "$directory/selection.json")
    jq -r '.external[]' "$directory/selection.json" > "$directory/external.txt"
    xhprof=$(jq -r '.xhprof' "$directory/selection.json")
    export XDG_CONFIG_HOME="$directory/config" XDG_CACHE_HOME="$directory/cache"
}

install_packages() {
    set -- $EXTENSIONS $PACKAGES
    if [ "$#" -eq 0 ]; then
        lock="$directory/empty-lock.json"
        jq -n --arg alpine "$release" --arg architecture "$architecture" \
            '{alpine: $alpine, architecture: $architecture, packages: {}}' > "$lock"
        return
    fi
    case "$architecture" in
        x86_64) arch=amd64 ;;
        aarch64) arch=arm64 ;;
        *) fail "Unsupported APK architecture: $architecture" ;;
    esac
    lock="$directory/build/apk/$release/$arch.json"
    jq -er '
        .packages | if type == "object" and all(to_entries[];
            (.key | test("^[a-zA-Z0-9+_.-]+\\z")) and
            (.value | test("^[a-zA-Z0-9+_.~-]+-r[0-9]+\\z"))) then
            to_entries | map(.key + "=" + .value) | join("\n")
        else error("Invalid APK pin") end
    ' "$lock" > "$directory/pins.txt"
    php "$directory/apk_lock.php" check "$baseline" "$lock"
    set --
    while IFS= read -r pin; do
        [ -z "$pin" ] || set -- "$@" "$pin"
    done < "$directory/pins.txt"
    # These public interfaces intentionally accept whitespace-separated lists.
    # shellcheck disable=SC2086
    apk add --no-cache --virtual .php-extension-runtime "$@" $PACKAGES
    set -- $EXTENSIONS
    if [ "$#" -gt 0 ]; then
        build_packages=$(sh "$directory/alpine_packages.sh" build)
        # shellcheck disable=SC2086
        apk add --no-cache --virtual .php-extension-build $build_packages
    fi
}

install_bundled() {
    set -- $core
    [ "$#" -gt 0 ] || return 0
    docker-php-source extract
    case " $core " in
        *' gd '*) docker-php-ext-configure gd --enable-gd --with-webp --with-jpeg \
            --with-xpm --with-freetype --with-avif ;;
    esac
    docker-php-ext-install "-j$jobs" "$@"
}

install_pie() {
    while IFS= read -r extension; do
        jq -er --arg name "$extension" '
            .pie_packages[$name] | [.package + ":" + .version] + .configure_options | .[]
        ' "$config" > "$directory/pie-arguments.txt"
        set --
        while IFS= read -r argument; do
            set -- "$@" "$argument"
        done < "$directory/pie-arguments.txt"
        if php /tmp/pie.phar --no-interaction --no-ansi install --no-cache \
            --skip-enable-extension "--make-parallel-jobs=$jobs" "$@"; then
            docker-php-ext-enable "$extension"
        else
            # Globbing is enabled only for PIE's diagnostic files.
            set +f
            for log in /tmp/pie_*_output_*; do
                [ ! -f "$log" ] || cat "$log" >&2
            done
            fail "PIE installation failed: $extension"
        fi
    done < "$directory/external.txt"
}

xhprof_record() {
    version=$(sed -n 's/^#define[[:space:]][[:space:]]*XHPROF_VERSION[[:space:]][[:space:]]*"\([^"]*\)".*/\1/p' \
        "$1")
    [ -n "$version" ] || fail 'Missing XHProf module version'
    case "$version" in
        *'
'*) fail 'Ambiguous XHProf module version' ;;
    esac
    jq --arg version "$version" '.xhprof + {version: $version}' "$2"
}

compile_xhprof() (
    cd "$1"
    phpize
    ./configure --with-php-config=/usr/local/bin/php-config
    make "-j$jobs"
    make install
)

install_xhprof() {
    [ "$xhprof" = true ] || return 0
    commit=$(jq -er '.xhprof.commit' "$config")
    checkout="$directory/xhprof"
    git init "$checkout"
    git -C "$checkout" remote add origin https://github.com/longxinH/xhprof.git
    git -C "$checkout" fetch --depth=1 origin "$commit"
    git -C "$checkout" checkout --detach FETCH_HEAD
    [ "$(git -C "$checkout" rev-parse HEAD)" = "$commit" ] || fail 'XHProf checkout mismatch'
    xhprof_record "$checkout/extension/php_xhprof.h" "$config" > /usr/local/lib/php/xhprof-build.json
    compile_xhprof "$checkout/extension"
    cp -a "$checkout/xhprof_lib" "$checkout/xhprof_html" /usr/local/lib/php/
    docker-php-ext-enable xhprof
}

cleanup() {
    set -- $EXTENSIONS
    if [ "$#" -gt 0 ]; then
        cp /tmp/composer /usr/local/bin/composer
        chmod 0755 /usr/local/bin/composer
    fi
    docker-php-source delete
    if [ "$#" -gt 0 ]; then
        apk del --no-network .php-extension-build
    fi
    apk del --no-network .php-installer-tools
    php "$directory/apk_lock.php" runtime "$baseline" "$lock"
    rm -rf "$directory" /root/.cache /root/.config/pie
}

# Sourcing defines functions without installing extensions.
if [ "${0##*/}" = install_extensions.sh ]; then
    read_configuration
    rm -f /usr/local/bin/pecl /usr/local/bin/pear /usr/local/bin/peardev
    install_packages
    install_bundled
    install_pie
    install_xhprof
    cleanup
fi
