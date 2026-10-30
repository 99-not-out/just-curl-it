# Shared helpers for the jci test scripts (run.sh, handlers.sh). Sourced,
# not executed. Sets REPO, CURL, DYLIB, PRELOAD_VAR; defines the
# section/ok/bad/assert_* vocabulary and `summary`.

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PASS=0
FAIL=0
declare -a FAILURES

case "$(uname -s)" in
    Darwin)
        BREW_CURL_DIR=$(brew --prefix curl 2>/dev/null)/bin
        CURL="$BREW_CURL_DIR/curl"
        [ -x "$CURL" ] || CURL=$(ls /opt/homebrew/Cellar/curl/*/bin/curl 2>/dev/null | head -1)
        DYLIB="$REPO/lib/intercept.dylib"
        PRELOAD_VAR=DYLD_INSERT_LIBRARIES
        ;;
    Linux)
        CURL=$(command -v curl)
        DYLIB="$REPO/lib/intercept.so"
        PRELOAD_VAR=LD_PRELOAD
        ;;
    *) echo "unsupported platform: $(uname -s)" >&2; exit 1 ;;
esac

if [ ! -x "${CURL:-/nonexistent}" ]; then
    echo "FAIL: no non-SIP curl available (brew install curl)" >&2
    exit 1
fi

section() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
skip()    { printf '  \033[33m~\033[0m %s\n' "$*"; }
ok()      { PASS=$((PASS+1)); printf '  \033[32m✓\033[0m %s\n' "$*"; }
bad()     { FAIL=$((FAIL+1)); FAILURES+=("$1"); printf '  \033[31m✗\033[0m %s\n' "$1"; [ -n "${2-}" ] && printf '    %s\n' "$2"; }

assert_eq()       { [ "$2" = "$3" ] && ok "$1" || bad "$1" "expected '$2', got '$3'"; }
assert_match()    { [[ "$3" == *"$2"* ]] && ok "$1" || bad "$1" "expected to contain '$2'; got '$3'"; }
assert_no_match() { [[ "$3" != *"$2"* ]] && ok "$1" || bad "$1" "expected NOT to contain '$2'; got '$3'"; }

# Print the tally and exit 0/1.
summary() {
    section "summary"
    local total=$((PASS+FAIL))
    if [ "$FAIL" -eq 0 ]; then
        printf '\033[32mall %d test(s) passed\033[0m\n' "$total"
        exit 0
    fi
    printf '\033[31m%d/%d failed:\033[0m\n' "$FAIL" "$total"
    local f
    for f in "${FAILURES[@]}"; do printf '  - %s\n' "$f"; done
    exit 1
}
