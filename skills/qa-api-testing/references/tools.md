# Tools

This skill installs nothing and downloads nothing. Every external tool runs only through the kernel, in a container from an image pinned by digest, with no network except the loopback it shares with a service the same run starts. The kernel never pulls an image. An engine that does not answer, or an image that is not on the machine, makes the run `blocked`: that record is the result. Never run the tool another way (a local install, `pip install`, `npx`, a binary found on the PATH).

Every kernel call is written out in full, from the project root:

`python3 .claude/skills/qa-api-testing/scripts/qa.py --role verifier --skill qa-api-testing <command>`

## Kernel commands used

| Command | Use |
|---|---|
| `init`, `config show`, `config set project.trust trusted --approval "<the user's words>"` | The workspace, and the user's approval to run the project's code here |
| `run --report junit:{evidence}/junit.xml -- python3 -B -m pytest -p no:cacheprovider .qa/work/api/<file>.py --junitxml={evidence}/junit.xml` | Your own in-process checks (`references/checks.md`). `-B` and `-p no:cacheprovider` keep `__pycache__` and `.pytest_cache` out of the project. Exit codes: 0 passed, 1 failed, 3 blocked, 4 inconclusive, 5 not_run |
| `run --container IMAGE@DIGEST [--service IMAGE@DIGEST --service-run SHELL] [--input .qa/work/api/FILE] --report FORMAT:{evidence}/FILE --shell -- 'SHELL'` | One tool run in a container. The project is copied to `/work`; an `--input` file under `.qa/work/` is at `/work/.qa-input/<path below .qa/work/>`; `{evidence}` is `/qa-evidence` inside. Only files a declared `--report` names are kept |
| `authorize --environment local --activity dast-passive [dast-active] --target 127.0.0.1 --approval "<the user's words>"` | The grant a Schemathesis run needs. Only the user's own words, only `127.0.0.1`, only `local`; it expires at the end of the day |
| `finding add --json .qa/work/api/<name>.json` | One finding (template in `references/checks.md`) |
| `state show [executions\|findings\|authorizations]`, `ledger verify` | What was recorded |

## The images

| Tool | Version | Image (use exactly this) |
|---|---|---|
| Spectral | 6.17.1 | `stoplight/spectral@sha256:3b05e6cd9d43375c454cc0c022e91b3285851043b05c3e738342ea3f84cde76d` (linux/amd64 only) |
| Schemathesis | 4.33.2 | `schemathesis/schemathesis@sha256:b6521357a7262c725cf10c6237fdf075b6d027219c0aca93c9930497d1824c85` |
| Hurl | 8.1.0 | `ghcr.io/orange-opensource/hurl@sha256:f8aed33658ba793ed53c5a5b2e57cadd9ec9322c8b56c550652c1f4cbaa241c5` |
| Service, Python API | 3 (slim) | `python@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928` |

A service in another runtime uses its image from `.claude/skills/qa-api-testing/scripts/_kernel/policies/container-images.json` (for example `node-lts`). An image not in that file is not used.

## Spectral: lint the contract

1. Function: lints an OpenAPI (or AsyncAPI) document against a ruleset; `spectral:oas` checks the document's structure and common contract hygiene (missing descriptions, tags, operation ids, invalid examples, unresolved references).
2. Use cases: the contract is the thing under review; before any dynamic run, to know the document is valid; to back a "the spec is broken" claim with rule ids and lines.
3. Compatibility: OpenAPI 2.0, 3.0 and 3.1, AsyncAPI 2.x; JSON or YAML; SARIF, JSON, JUnit and text output.
4. Dependencies: the image above (Node inside); no service, no network. The ruleset `assets/spectral.yaml` (`extends: spectral:oas`).
5. Alternatives: Redocly CLI `lint` (MIT), vacuum (MIT), openapi-spec-validator (Apache-2.0, validation only).
6. License, costs, restrictions: Apache-2.0, free. The CLI may send anonymous usage data; `DO_NOT_TRACK=1 SCARF_NO_ANALYTICS=true` turns it off, and the container has no network anyway.
7. Reason selected: the reference ruleset for OpenAPI, with SARIF output the kernel stores and turns into provisional findings.

Copy the ruleset first (`python3 .claude/skills/qa-api-testing/scripts/endpoint_inventory.py assets`), then:

```qa-leg spectral grant=none report=sarif
python3 .claude/skills/qa-api-testing/scripts/qa.py --role verifier --skill qa-api-testing run --container stoplight/spectral@sha256:3b05e6cd9d43375c454cc0c022e91b3285851043b05c3e738342ea3f84cde76d --timeout 300 --input .qa/work/api/spectral.yaml --report 'sarif:{evidence}/spectral.sarif' --shell -- 'DO_NOT_TRACK=1 SCARF_NO_ANALYTICS=true spectral lint --ruleset /work/.qa-input/api/spectral.yaml --format sarif --output /qa-evidence/spectral.sarif /work/<spec>'
```

`<spec>` is the contract's path from the project root. Spectral exits 0 even with warnings; the kernel reads the SARIF, so a run with alerts is `failed` and each alert is a provisional finding. Quote rule ids and counts from the stored SARIF, never from memory.

## Schemathesis: check the running API against the contract

1. Function: generates requests from the OpenAPI document and checks every response against it: status codes the operation documents, response schemas, content types, server errors, and (with the stateful phase) links between operations.
2. Use cases: "does the API do what the spec says", at scale; finding undocumented status codes, schema violations and 5xx answers on inputs nobody wrote by hand.
3. Compatibility: OpenAPI 2.0, 3.0 and 3.1, GraphQL; any HTTP API it can reach. Here it reaches only the service started in the same run, on `127.0.0.1`.
4. Dependencies: the image above; a service image and the shell text that starts the API (`<serve>`); a grant recorded with the user's words: `dast-passive` for the baseline, `dast-active` for the fuzzing and stateful phases.
5. Alternatives: RESTler (MIT, stateful fuzzing, heavier), EvoMaster (LGPL-3.0, search-based), Dredd (MIT, archived), hand-written contract tests (`references/checks.md`).
6. License, costs, restrictions: MIT, free. The hosted Schemathesis service is paid and is not used: nothing leaves the container.
7. Reason selected: property-based contract checking with JUnit and HAR output, bounded by phases, a request cap and a rate cap.

The guard reads the command line: `--phases=examples,coverage` is `dast-passive`; any other phase, or no `--phases`, is `dast-active`. Keep the caps on the command line in both forms.

Baseline (copy `assets/schemathesis-baseline.toml` first; grant: `dast-passive`). The HAR file is not a report format the kernel keeps, so the run prints it as its own stdout, stored as the run's `stdout.log`:

```qa-leg schemathesis-baseline grant=dast-passive report=junit
python3 .claude/skills/qa-api-testing/scripts/qa.py --role verifier --skill qa-api-testing run --container schemathesis/schemathesis@sha256:b6521357a7262c725cf10c6237fdf075b6d027219c0aca93c9930497d1824c85 --service python@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 --service-run '<serve>' --service-user 65534:65534 --timeout 600 --input .qa/work/api/schemathesis-baseline.toml --report 'junit:{evidence}/schemathesis.xml' --shell -- 'sleep 3; st --config-file /work/.qa-input/api/schemathesis-baseline.toml run /work/<spec> --url=http://127.0.0.1:8080 --phases=examples,coverage -H "Authorization: Bearer <token>" --report=junit,har --report-junit-path=/qa-evidence/schemathesis.xml --report-har-path=/qa-evidence/schemathesis.har 1>&2; status=$?; cat /qa-evidence/schemathesis.har; exit $status'
```

All phases (grant: `dast-passive` and `dast-active`):

```qa-leg schemathesis-active grant=dast-active report=junit
python3 .claude/skills/qa-api-testing/scripts/qa.py --role verifier --skill qa-api-testing run --container schemathesis/schemathesis@sha256:b6521357a7262c725cf10c6237fdf075b6d027219c0aca93c9930497d1824c85 --service python@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 --service-run '<serve>' --service-user 65534:65534 --timeout 900 --report 'junit:{evidence}/schemathesis.xml' --shell -- 'sleep 3; st run /work/<spec> --url=http://127.0.0.1:8080 --phases=examples,coverage,fuzzing,stateful --max-examples=20 --rate-limit=10/s --max-failures=50 --request-timeout=5 -H "Authorization: Bearer <token>" --report=junit --report-junit-path=/qa-evidence/schemathesis.xml'
```

- `<serve>` starts the API on `127.0.0.1:8080` from the copy of the project at `/work`, in the foreground, for example `cd /work && exec python3 -m <package>.server --host 127.0.0.1 --port 8080`. The service runs as an unprivileged user (`--service-user 65534:65534`) and shares only loopback with the tool.
- `<token>` is a test user's token that the project's own docs or test fixtures give. Drop the `-H` option when the API needs none. Never a real credential.
- The JUnit report has one test case per operation and check; a failure's text names the check (for example `ST103`, an undocumented status code: "Received: 200, Documented: 201, 400"). The kernel stores it; quote failures from it.

## Hurl: scripted requests with assertions

1. Function: runs plain-text HTTP request files with assertions on status, headers and body, and captures values between requests.
2. Use cases: smoke checks of a running API; a fixed scenario (log in, create, read back); reproducing one finding as a request anyone can re-run.
3. Compatibility: any HTTP/1.1 or HTTP/2 API; JUnit, TAP and HTML reports.
4. Dependencies: the image above (libcurl inside), a service started in the same run, and a `.hurl` file (`assets/smoke.hurl`, adapted to the inventory) passed with `--input`. No grant: it sends only the requests written in the file.
5. Alternatives: Newman for Postman collections (Apache-2.0), Bruno CLI (MIT), HTTPie (BSD-3-Clause), the in-process checks of `references/checks.md`.
6. License, costs, restrictions: Apache-2.0, free.
7. Reason selected: a small static binary whose request files are readable evidence, with JUnit output the kernel stores.

```qa-leg hurl grant=none report=junit
python3 .claude/skills/qa-api-testing/scripts/qa.py --role verifier --skill qa-api-testing run --container ghcr.io/orange-opensource/hurl@sha256:f8aed33658ba793ed53c5a5b2e57cadd9ec9322c8b56c550652c1f4cbaa241c5 --service python@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 --service-run '<serve>' --service-user 65534:65534 --timeout 300 --input .qa/work/api/smoke.hurl --report 'junit:{evidence}/hurl.xml' --shell -- 'sleep 3; hurl --test --report-junit /qa-evidence/hurl.xml --variable base=http://127.0.0.1:8080 /work/.qa-input/api/smoke.hurl'
```

## When a tool cannot run

| The run's record | What it means | What you say |
|---|---|---|
| `blocked`, `dependency_unavailable`, limitation `container_engine_unavailable` | No container engine answered | "<tool> did not run: no container engine on this machine (EXE-…)". No result, no estimate |
| `blocked`, image not present | The pinned image is not on the machine; the kernel never pulls | The image the user would need, by digest |
| `blocked`, `not_authorized` | No grant for `dast-passive`/`dast-active` | Ask for the user's approval in their words, for `127.0.0.1` only |
| `failed` | The tool ran and reported failures or alerts | Quote them from the stored report |

A blocked tool leaves its operations `blocked` or `not_run` in the inventory, with the reason. Your in-process checks still run (`references/checks.md`): they are your evidence, under their own name, never relabelled as the tool's.
