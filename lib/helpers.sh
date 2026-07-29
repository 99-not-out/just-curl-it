# jci helpers -- sourced by `jci use`'s rcfile, and by scripts inside the
# activated env. POSIX-ish; works in bash and zsh.
#
# Assumes the env vars exported by `jci use`:
#   JCI_LIB, JCI_PRELOAD_VAR, JCI_SHELL, INTERCEPT_CONFIG, JCI_ENV.

# Run a command with DYLD_INSERT_LIBRARIES (or LD_PRELOAD) re-established.
#
# Use this from inside scripts whose shebang triggers SIP on macOS
# (notably `#!/usr/bin/env bash`): /usr/bin/env is SIP-protected, so the
# kernel strips DYLD_* before the script even starts running. By the time
# any curl inside the script fires, the shim is gone.
#
# Inside the interactive `jci use` shell this is a no-op -- DYLD is already
# alive, so direct `curl ...` works there.
#
#   jci-run 'curl -s http://mylocal.shim/routes | jq'
jci-run() {
    if [ -z "${JCI_PRELOAD_VAR-}" ] || [ -z "${JCI_LIB-}" ] || [ -z "${JCI_SHELL-}" ]; then
        echo "jci-run: not inside an activated env (run 'jci use <env>')" >&2
        return 1
    fi
    env "$JCI_PRELOAD_VAR=$JCI_LIB" "$JCI_SHELL" -c "$*"
}

# Print the routes for the active env, one per line, columns aligned.
jci-routes() {
    if [ ! -f "${INTERCEPT_CONFIG-}" ]; then
        echo "jci: no INTERCEPT_CONFIG set (run 'jci use <env>')" >&2
        return 1
    fi
    grep -vE '^[[:space:]]*(#|$)' "$INTERCEPT_CONFIG" | column -t
}

# For each route in the active env, fetch /_info and print it. Routes whose
# handlers don't expose /_info just show whatever they returned -- useful
# for discovering the URL menu of an unfamiliar env.
#
# Goes through jci-run so this works from a script whose shebang lost DYLD_*,
# not just from the interactive shell.
jci-info() {
    if [ ! -f "${INTERCEPT_CONFIG-}" ]; then
        echo "jci: no INTERCEPT_CONFIG set (run 'jci use <env>')" >&2
        return 1
    fi
    awk 'NF && !/^[[:space:]]*#/ {print $1}' "$INTERCEPT_CONFIG" \
        | while read -r host; do
            printf '\n=== %s ===\n' "$host"
            out=$(jci-run "curl -s --max-time 3 'http://$host/_info'" 2>/dev/null)
            if [ -n "$out" ]; then
                echo "$out" | jq . 2>/dev/null || echo "$out"
            else
                echo "(no response)"
            fi
        done
}
