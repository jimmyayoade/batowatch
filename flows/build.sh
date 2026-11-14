#!/bin/sh
# Build bw-flows as a static binary (musl, in an Alpine container), for the
# machine's own architecture. Used by CI and for local builds.
#   flows/build.sh            -> flows/bw-flows-$(uname -m)
#   flows/build.sh OUTPUT
set -eu

cd "$(dirname "$0")/.."
out="${1:-flows/bw-flows-$(uname -m)}"
image="alpine:3.21"

docker run --rm -v "${PWD}:/src" -w /src "${image}" sh -euc '
    apk add --no-cache gcc musl-dev linux-headers >/dev/null
    gcc -O2 -static -s -Wall -Wextra -Werror -o "$1" flows/bw-flows.c
    chown "$2" "$1"
' sh "${out}" "$(id -u):$(id -g)"

file "${out}" 2>/dev/null || true
ls -l "${out}"
