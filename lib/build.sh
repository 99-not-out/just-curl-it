#!/usr/bin/env bash
# Build the intercept shim for the host platform.
#   Linux  -> intercept.so   (LD_PRELOAD)
#   Darwin -> intercept.dylib (DYLD_INSERT_LIBRARIES via __interpose section)
set -euo pipefail
cd "$(dirname "$0")"

case "$(uname -s)" in
    Linux)
        gcc -O2 -Wall -Wextra -shared -fPIC -o intercept.so intercept.c -ldl
        echo "built $(pwd)/intercept.so"
        ;;
    Darwin)
        clang -O2 -Wall -Wextra -dynamiclib -o intercept.dylib intercept.c
        echo "built $(pwd)/intercept.dylib"
        ;;
    *)
        echo "unsupported platform: $(uname -s)" >&2
        exit 1
        ;;
esac
