#!/bin/sh
# Test extension classification, pin validation, XHProf metadata,
# and argument, working-directory, and failure handling.
set -eu

# Load installer functions and prepare isolated test files.
if ! command -v jq > /dev/null; then
    apk add --no-cache --virtual .installer-test-tools jq
fi

root=$(CDPATH='' cd -P "$(dirname "$0")/.." && pwd)
# shellcheck source=scripts/install_extensions.sh
. "$root/scripts/install_extensions.sh"

temporary=$(mktemp -d)
trap 'rm -rf "$temporary"' EXIT HUP INT TERM

config="$root/build/php/8.4.json"
plan="$temporary/plan.json"

expect_failure() {
    if ( "$@" ) > /dev/null 2>&1; then
        echo "Expected installer rejection: $*" >&2
        exit 1
    fi
}

# Classify bundled, PIE, and source-built extensions separately.
extension_plan "$config" 80400 'bcmath redis xhprof' > "$plan"
jq -e '
    .core == ["bcmath"] and
    .external == ["redis"] and
    .xhprof == true
' "$plan" > /dev/null

# An empty selection must produce no installation targets.
extension_plan "$config" 80400 '' > "$plan"
jq -e '
    .core == [] and
    .external == [] and
    .xhprof == false
' "$plan" > /dev/null

# Opcache is installable on PHP 8.4, but built into PHP 8.5.
extension_plan "$config" 80400 opcache > "$plan"
jq -e '.core == ["opcache"]' "$plan" > /dev/null
expect_failure extension_plan "$config" 80500 opcache

# Reject invalid extension names, PIE packages, versions, and configure options.
expect_failure extension_plan "$config" 80400 'redis;echo unsafe'

for change in \
    '.package = "not-a-package"' \
    '.version = "latest"' \
    '.configure_options = ["--invalid;command"]'
do
    jq ".pie_packages.redis |= ($change)" "$config" > "$temporary/invalid.json"
    expect_failure extension_plan "$temporary/invalid.json" 80400 redis
done

# XHProf source pins must use a full commit rather than a branch name.
jq '.xhprof.commit = "main"' "$config" > "$temporary/invalid.json"
expect_failure extension_plan "$temporary/invalid.json" 80400 xhprof

# Preserve the source tag/commit while recording the module version independently.
printf '#define XHPROF_VERSION "2.3.9"\n' > "$temporary/header.h"
xhprof_record "$temporary/header.h" "$config" > "$temporary/record.json"
jq -e --slurpfile manifest "$config" '
    . == ($manifest[0].xhprof + {version: "2.3.9"})
' "$temporary/record.json" > /dev/null

# Reject headers with either a missing or an ambiguous module version.
: > "$temporary/header.h"
expect_failure xhprof_record "$temporary/header.h" "$config"

printf '%s\n' \
    '#define XHPROF_VERSION "1.0.0"' \
    '#define XHPROF_VERSION "2.0.0"' \
    > "$temporary/header.h"
expect_failure xhprof_record "$temporary/header.h" "$config"

# Stub build commands to observe their arguments and working directories.
mkdir "$temporary/bin" "$temporary/work"

cat > "$temporary/bin/php" <<'SH'
#!/bin/sh
jq -n --args '$ARGS.positional' -- "$@" > "$ARGUMENT_LOG"
exit "${COMMAND_STATUS:-0}"
SH

printf '#!/bin/sh\nexit 0\n' > "$temporary/bin/phpize"
ln -s phpize "$temporary/bin/make"
ln -s phpize "$temporary/bin/docker-php-ext-enable"

# shellcheck disable=SC2016
printf '#!/bin/sh\npwd > "$WORKING_DIRECTORY"\n' > "$temporary/work/configure"

chmod +x \
    "$temporary/bin/php" \
    "$temporary/bin/phpize" \
    "$temporary/work/configure"

export PATH="$temporary/bin:$PATH"
export ARGUMENT_LOG="$temporary/arguments.json"
export WORKING_DIRECTORY="$temporary/cwd.txt"
directory="$temporary"
jobs=1

# Pass the manifest's exact PIE package version and configure options.
# Skip the seven fixed arguments for the PIE command and installer flags.
printf 'redis\n' > "$directory/external.txt"
install_pie
jq -e --slurpfile manifest "$config" '
    .[7:] == (
        $manifest[0].pie_packages.redis |
        [.package + ":" + .version] + .configure_options
    )
' "$ARGUMENT_LOG" > /dev/null

# Pass spaces and shell syntax literally as a single command argument.
# Bypass pin validation in this fixture to exercise argument handling directly.
# shellcheck disable=SC2016
literal='spaces; $(printf unexpected)'
jq --arg literal "$literal" '
    .pie_packages.redis.configure_options = [$literal]
' "$config" > "$temporary/literal.json"

config="$temporary/literal.json"
install_pie
jq -e --arg literal "$literal" '.[-1] == $literal' "$ARGUMENT_LOG" > /dev/null

# Execute XHProf compilation commands inside the requested source directory.
compile_xhprof "$temporary/work"
[ "$(cat "$WORKING_DIRECTORY")" = "$temporary/work" ]

# A failed command must cause installation to fail.
export COMMAND_STATUS=17
expect_failure install_pie

echo 'Installer classification, validation, metadata, and command handling passed'
