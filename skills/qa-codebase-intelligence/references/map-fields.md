# The fields of `codebase-map.json`

The schema is `codebase-map.schema.json` in the vendored kernel (`scripts/_kernel/schemas/`). `scripts/write_map.py write` fills `schema_version`, `commit`, `dirty`, `tree` and `generated_at` itself. You add the rest with the authoring flags of `write` (`--summary/--cite`, `--framework`, `--entry`, `--data-store`, `--integration`, `--critical`, `--unknown`, `--purpose`; see the top of `write_map.py`). Every list entry needs `evidence`: one or more `{"path": "<file>", "lines": "10-20"}` (lines optional) or `{"exe": "EXE-..."}` for a command the kernel recorded.

| Field | What goes in it | Where it comes from |
|---|---|---|
| `summary` | One factual sentence on what the project is, with evidence. | What the manifests, the package names and the entry code show. Not the marketing line of a README, unless you cite it as the README's claim. |
| `stack[]` | `language`, `frameworks` (names only), `evidence` (manifest and a source file). | `draft` gives languages from file extensions and manifests. Add a framework only when a manifest dependency or an import shows it. |
| `entry_points[]` | `kind` (main, cli, route, handler, worker, scheduler, consumer, library-api, other), `name`, `path`, `evidence`. | `_entry_hints`, manifest `main` / `bin` / `scripts.start`, and files you opened. A pure library has none: leave it empty and say so under `unknowns`. |
| `modules[]` | `path`, `language`, `purpose` (one line, read from the file), `imports` (repository paths), `fan_in`, `fan_out`, `approximate`, `evidence`. | `draft` fills everything, with `purpose` taken from the file's own first description line (or "No description in the file."). Override it with `--purpose` only where you read more. |
| `data_stores[]` | `kind`, `name`, `evidence`. | A driver or ORM import, a DSN or configuration file, a migrations folder, a schema file. Never from a name, a README claim or the user's belief alone. |
| `integrations[]` | `kind` (http-api, queue, sdk, ...), `name`, `evidence`. | HTTP or SDK client imports and calls, queue clients, environment variable names that hold URLs or keys. Names of variables only, never values. |
| `critical_components[]` | `path`, `tags` (auth, payments, pii, persistence-writes, money-arithmetic, external-calls, other), `reason`, `evidence`. | Files you read. A tag is a statement: it needs the file and a reason you can point at. |
| `hotspots[]` | `path`, `commits`, `lines_changed`, `complexity`, `score`, `evidence`. | Copy from `draft`; do not recompute. Say when history is too short. |
| `tests[]` | `framework`, `command` (or null), `files`, `test_count`, `evidence`. | `draft` from `inventory_tests.py`; `command` is declared by the project or inferred, and the answer says which. |
| `unknowns[]` | `topic`, `why`, `searched` (what you looked at). | Everything asked or expected that no file settled: a database the user mentioned, an entry point, a dependency graph for a language with no reader. |

## Rules of thumb

- Absent is not "no": `unknowns` says "none found, searched X", never "there is no X" as a fact about the system.
- A path in `evidence` must exist in the repository; `write` refuses otherwise. Do not cite `.qa/` files or the skill's own files.
- Duplication, dead code and coupling findings from an optional analyzer belong in `modules[]` or `critical_components[]` only when a path shows them; the tool's execution ID goes in `evidence`.
- Keep `purpose`, `reason` and `name` to one line, at most 600 characters.
