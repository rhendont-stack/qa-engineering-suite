# Optional analyzers and how they are cited

None of these is required, and none is installed by this skill. Check for one with `command -v <tool>` (or `<tool> --version`); a missing tool is stated in the answer, and the bundled scripts are used instead.

| Need | Tool | Check | Use |
|---|---|---|---|
| Complexity per function | lizard | `lizard --version` | `qa run -- lizard <dir> --csv` gives a recorded execution; use it to replace the script's decision-point count in `hotspots[].complexity` and cite the `EXE` in `evidence`. |
| JS or TS dependency graph | madge or dependency-cruiser | `madge --version` | `qa run -- madge --json <dir>` records the graph; use it to correct `modules[].imports` and set `approximate` to false for those modules. |
| Python import contracts | pydeps, import-linter | `pydeps --version` | Not needed: the bundled reader uses `ast` and is exact for Python. |
| Go, Rust, Java graphs | `go list -deps`, `cargo metadata`, `jdeps` | `go version`, `cargo --version`, `jdeps --version` | Run through `qa run` where the project is authorized to run its toolchain; otherwise leave those modules approximate. |
| Duplicated code | jscpd | `jscpd --version` | `qa run -- jscpd <dir> --reporters json`; cite the execution on each module it names. |
| Dead code | knip (JS), vulture (Python) | `vulture --version` | Report as candidates, cited by execution; a name used only through reflection or a framework can be a false positive. |

`qa run` needs the project to be trusted for running its own tools; mapping itself needs no run. If trust is not granted, skip the analyzers and say so under `unknowns`.

Without an analyzer, for a language the bundled reader does not cover (Ruby, PHP, C#, Rust, Kotlin), list its files as modules with empty `imports` and `approximate: true`, and put "dependency graph for <language>" under `unknowns`.
