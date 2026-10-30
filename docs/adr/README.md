# Architecture decision records

Short notes on the decisions that make jci what it is — the ones you'd
otherwise have to reverse-engineer from a C comment or a README aside.
One file per decision, numbered, never edited after acceptance except to
change the status line. To change your mind, write a new one and mark the
old one `Superseded by NNNN`.

| # | decision | status |
|---|---|---|
| [0001](0001-route-on-sentinel-ips.md) | Route on sentinel loopback IPs, not ports | Accepted |
| [0002](0002-handlers-are-forked-stdio-filters.md) | Handlers are per-request forked stdin/stdout filters; no daemon | Accepted |
| [0003](0003-core-is-bash-and-c.md) | The core is bash + C; Python appears only as a stdlib-only handler | Accepted |
| [0004](0004-method-gate-enforced-in-shim.md) | Method allow-lists are enforced by the shim, not by handlers | Accepted |
| [0005](0005-get-is-read-only.md) | `GET` must not mutate; handlers enforce it at the backend | Accepted |
| [0006](0006-handlers-are-an-opt-in-library.md) | Handlers are an opt-in library with per-handler venvs, managed by `jci handler` | Proposed |

## Writing one

Copy `template.md`. Keep it under a screen. Context is *why the question
came up*, Decision is *what we do*, Consequences is *what that costs and
buys* — including the honest downsides, in the same voice as the README's
"What it's not". Date it. Reference the code it governs by path.

What warrants an ADR: anything that changes how jci works rather than
whether it works — routing, the process model, where enforcement lives,
what the core is written in, the handler contract, the install layout.
Bug fixes, new demo handlers and refactors that leave those intact don't.

`make hooks` installs a pre-commit reminder that fires when core files
change with no ADR staged. It warns; `JCI_ADR_STRICT=1` makes it block.
