# 0003. The core is bash + C; Python appears only as a stdlib-only handler

- Status: Accepted; amended by 0006
- Date: 2026-10-30
- Governs: `bin/jci`, `lib/helpers.sh`, `lib/build.sh`, `lib/intercept.c`, `handlers/run`, `handlers/lib/jci.py`, `Makefile`

## Context

The question "should a CLI you just want to get running depend on Python?"
came up while reviewing dependencies. Taking stock: the env/CLI layer
(`jci`, the helpers, the build) is bash; the interposer is C; the only
Python in the core install is `handlers/proxy.py`. Everything else Python
is the demos (a venv with protocol client libraries) or test fixtures.

`proxy.py` is stdlib-only (`configparser`, `http.client`, `ssl`,
`subprocess`) and runs under whatever `python3` is on `PATH`, including
macOS's system interpreter: handlers run with the shim unset, so SIP is
irrelevant to them. Building the shim already requires a C toolchain,
which on macOS is Xcode CLT, which is what provides `/usr/bin/python3`.
The marginal cost of Python in the core is therefore nil.

The one credible alternative is a proxy handler written as bash around
`curl`, which fits the project's name and handles TLS at least as well.
It would trade twenty lines of `http.client` for seventy lines of
response-header hygiene in `sed` (curl's `-i` prints raw headers over a
decoded body, so `Transfer-Encoding` lies; status lines need forcing to
`HTTP/1.1`), and would make the carefully worded error messages harder to
keep sharp.

## Decision

The user-facing core stays bash + C. `bin/jci` and `lib/helpers.sh` never
require Python; if they ever need to, that is a new decision. Bundled
handlers may be Python provided they are stdlib-only and run under any
`python3` ≥ 3.8 without a venv. Anything needing third-party packages
belongs in `demos/` behind its own venv. A curl-based proxy, if written,
ships alongside `proxy.py` as a second handler, not as a replacement.

*Amended by ADR 0006:* handlers with third-party packages now live in the
library under `handlers/lib/`, opt-in, one venv each, managed by `jci
handler`. "Never require Python" is to be read as *jci's own logic is
bash; Python is the thing it manages* — `jci ls`, `use` and `doctor` work
with no `python3` on `PATH`. The core-language decision itself stands.

## Consequences

- First run needs: `make`, a C compiler, bash, curl (Homebrew's on macOS),
  and a `python3` the toolchain already brought in. No pip, no venv.
- The real first-run friction on macOS — Homebrew `zsh` and `curl` because
  SIP strips the preload var from `/bin/*` and `/usr/bin/*` — is inherent
  to the mechanism and unaffected by this decision.
- Handler authors can pick any language; the core's choice constrains only
  what ships in `handlers/`.
- The demos' venv prefers Homebrew Python only because it is newer than
  Apple's 3.9, not for any SIP reason. Docs that implied otherwise were
  corrected with this ADR.
