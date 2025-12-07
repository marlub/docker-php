#!/usr/bin/env sh
set -e

while [ $# -gt 0 ]; do
  case "$1" in
    --image=*)
      IMAGE="${1#*=}"
      ;;
    --node=*)
      NODE_VERSION="${1#*=}"
      ;;
    --extensions=*)
      EXTENSIONS="${1#*=}"
      ;;
    *)
      printf "***************************\n"
      printf "* Error: Invalid argument.*\n"
      printf "***************************\n"
      exit 1
  esac
  shift
done

echo "> Test if all extensions are disabled by default"
output=$(docker run --rm $dockerArgs $IMAGE php -m)
output=$(echo "$output" | tr '[:upper:]' '[:lower:]')

for ext in $EXTENSIONS; do
    if echo "$output" | grep -q "$ext" 2>/dev/null; then
        >&2 echo "$ext extension is loaded by default!"
        exit 1
    else
        echo "$ext extension is not loaded."
    fi
done

echo "> Test if all extensions can be loaded"
dockerArgs=""
for ext in $EXTENSIONS; do
    ext=$(echo "$ext" | tr '[:lower:]' '[:upper:]')
    dockerArgs="$dockerArgs -e PHP_EXTENSION_$ext=true"
done

output=$(docker run --rm $dockerArgs $IMAGE php -m)
output=$(echo "$output" | tr '[:upper:]' '[:lower:]')

for ext in $EXTENSIONS; do
    if echo "$output" | grep -q "$ext" 2>/dev/null; then
        echo "$ext extension is loaded."
    else
        >&2 echo "$ext extension is not loaded!"
        exit 1
    fi
done

echo "> Test if node $NODE_VERSION is shipped and can be executed"
if [ "$NODE_VERSION" != "" ]; then
    output=$(docker run --rm $IMAGE node -v)
    if echo "$output" | grep -q "v$NODE_VERSION" 2>/dev/null; then
        echo $output
    else
        >&2 echo "Node version $NODE_VERSION not found!"
        exit 1
    fi
fi
