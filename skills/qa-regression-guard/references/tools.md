# Tools

Nothing here is installed by this skill. A missing tool makes a run `blocked`; report what is missing and what the user could do. Every kernel call is written out in full: `python3 .claude/skills/qa-regression-guard/scripts/qa.py --role verifier --skill qa-regression-guard <command>`, or, inside a suite worktree, `cd <root>/.qa/work/wt-<hex> && python3 ../../../.claude/skills/qa-regression-guard/scripts/qa.py --role verifier --skill qa-regression-guard <command>`, where `<root>` is the project root's absolute path. The `cd` stays in effect for later calls; return with `cd <root> && …`.

## Kernel commands used

| Command | Use |
|---|---|
| `init`, `config show`, `config set project.trust trusted --approval "<the user's words>"` | The workspace and the user's approval to run tests here |
| `detect tests` | The project's test commands, ranked: CI, documented, inferred |
| `worktree add <commit>`, `worktree remove <path>` | A suite worktree at `.qa/work/wt-<12 hex>` holding exactly that commit; a run started inside it is recorded at that commit |
| `run --scope full --report FORMAT:PATH -- <command>` | One recorded run. Exit code: 0 passed, 1 failed, 3 blocked, 4 inconclusive, 5 not_run |
| `run --container IMAGE@DIGEST -- <command>` | The same in a container with no network, from an archive of the project root; refused inside a worktree |
| `compare BASE HEAD` | Test-by-test difference of two recorded runs: `new_failures`, `fixed`, `appeared`, `disappeared`, `flaky`. Read-only |
| `finding add --json FILE`, `fix add --json FILE`, `revalidate FIX --f2p B:H --p2p B:H --defect-test ID` | The records of this skill |
| `gate evaluate --profile change` | The change gate's verdict, recorded |
| `state show [KEY]` | The state, read through the kernel |

## Report flags

`{evidence}` in a path is replaced by the run's evidence directory; keep reports there so the worktree stays clean.

| Framework | Command in `run … --` | `--report` |
|---|---|---|
| pytest | `python -B -m pytest -p no:cacheprovider '--junitxml={evidence}/junit.xml'` | `'junit:{evidence}/junit.xml'` |
| unittest (no pytest) | not structured: the run rests on the exit code alone; say so | none |
| Jest | `npx jest --reporters=default --reporters=jest-junit` (only if `jest-junit` is already installed; it writes `junit.xml` in the working directory) | `junit:junit.xml` |
| Vitest | `npx vitest run --reporter=junit '--outputFile.junit={evidence}/junit.xml'` | `'junit:{evidence}/junit.xml'` |
| Maven | `mvn -o test` | `'junit:target/surefire-reports/TEST-*.xml'` |
| Gradle | `./gradlew cleanTest test` | `'junit:build/test-results/test/*.xml'` |
| go | `gotestsum --junitfile {evidence}/junit.xml -- ./...` if installed | `'junit:{evidence}/junit.xml'` |

Use the same command and scope at both commits; `compare` refuses runs that are not comparable.

## Contract diffs through `run --container`

`diff_impact.py` lists changed contracts with the tool that compares two versions:

| Contract | Tool | Command shape |
|---|---|---|
| OpenAPI | oasdiff | `oasdiff breaking <base spec> <head spec> --fail-on ERR` |
| GraphQL schema | GraphQL Inspector | `graphql-inspector diff <base schema> <head schema>` |
| Prisma schema | prisma | `prisma migrate diff --from-schema-datamodel <base> --to-schema-datamodel <head>` |

These run only through the kernel, in a container, never directly:

`python3 .claude/skills/qa-regression-guard/scripts/qa.py --role verifier --skill qa-regression-guard run --container <image>@sha256:<digest> -- oasdiff breaking <base spec> <head spec> --fail-on ERR`

- The image is pinned by digest and must already be on the machine: the kernel never pulls one. The suite's pinned images are listed in `scripts/_kernel/policies/container-images.json`; an oasdiff image is not among them, so the user supplies its digest and pulls it themselves. The kernel starts the command through `sh` in the image, so the image needs a shell.
- The container has no network and receives an archive of the project root: tracked and untracked files, without `.git` and `.qa`, capped in size (`project_too_large` above the cap). Both spec versions must therefore be files in the project tree; the base version cannot come from git history inside the container, and this skill never writes one into the project.
- A container run cannot start inside a suite worktree (`container_in_worktree`).
- Exit code 1 with breaking changes listed is a `failed` run: report the breaking changes as findings (`functional`, the contract path as `component.path`, the run's EXE as evidence).

Fallback when no image is available or the base spec is not in the tree: read the contract's diff with `python3 .claude/skills/qa-regression-guard/scripts/git_read.py diff --base <base> --head <head> --path <spec>`, list every removed or renamed path, field, parameter or enum value and every field that became required, and say in the answer that the comparison was by reading, not by a tool. A breaking change found by reading is a `suspected` finding.
