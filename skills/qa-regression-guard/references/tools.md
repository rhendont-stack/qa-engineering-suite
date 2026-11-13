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

For an OpenAPI contract the suite pins oasdiff by digest (`oasdiff` in `scripts/_kernel/policies/container-images.json`). Write the base version of the spec to `.qa/work/contract/base.yaml` (the kernel's scratch area, outside the project tree), keep the head version in the project tree, then run from the project root, with `<spec>` the head spec's path:

```qa-leg contract-diff
python3 .claude/skills/qa-regression-guard/scripts/qa.py --role verifier --skill qa-regression-guard run --container tufin/oasdiff@sha256:ff02af37a254b5199b9d72e7bba93dae5837a9119d59459168d1398893b47aea --timeout 300 --input .qa/work/contract/base.yaml --allow-empty --report 'junit:{evidence}/oasdiff.xml' --shell -- 'oasdiff breaking .qa-input/contract/base.yaml <spec> --fail-on ERR --format junit > {evidence}/oasdiff.xml'
```

- The image is pinned by digest and must already be on the engine: the kernel never pulls one. The container has no network, and `--input` places the base spec at `/work/.qa-input/contract/base.yaml`.
- The container receives an archive of the project root: tracked and untracked files, without `.git` and `.qa`, capped in size (`project_too_large` above the cap). The head spec is therefore a file in the project tree; the base version cannot come from git history inside the container, which is why it travels as `--input`.
- A container run cannot start inside a suite worktree (`container_in_worktree`).
- The kernel reads the JUnit file: breaking changes make a `failed` run, one test case per finding; no change is one passing case. `--allow-empty` keeps a spec pair the tool lists nothing for from being inconclusive.
- Exit code 1 with breaking changes listed is a `failed` run: report the breaking changes as findings (`functional`, the contract path as `component.path`, the run's EXE as evidence).

Fallback when no image is available or the base spec is not at `.qa/work/contract/base.yaml`: read the contract's diff with `python3 .claude/skills/qa-regression-guard/scripts/git_read.py diff --base <base> --head <head> --path <spec>`, list every removed or renamed path, field, parameter or enum value and every field that became required, and say in the answer that the comparison was by reading, not by a tool. A breaking change found by reading is a `suspected` finding.
