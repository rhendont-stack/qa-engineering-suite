# Tools

This skill installs nothing on this machine. Every external tool runs only through the kernel, in a container from an image pinned by digest. The container has no network, except the loopback it shares with a service the same run starts, and except OSV-Scanner's install step, which fetches its advisory database under its own grants. The kernel never pulls an image. An engine that does not answer, or an image that is not on the machine, makes the run `blocked`: that record is the result. Never run the tool another way (a local install, `brew`, `pip install`, `npx`, a binary found on the PATH, a hosted scanner).

Every kernel call is written out in full, from the project root:

`python3 .claude/skills/qa-security/scripts/qa.py --role auditor --skill qa-security <command>`

## Kernel commands used

| Command | Use |
|---|---|
| `init`, `config show` | The workspace: `project.trust`, the declared environments, the grants |
| `run --container IMAGE@DIGEST [--service IMAGE@DIGEST --service-run SHELL --service-user UID:GID] [--input .qa/work/security/FILE] --report FORMAT:{evidence}/FILE --shell -- 'SHELL'` | One tool run in a container. The project is copied to `/work` (files git lists; `.qa/` stays out); an `--input` file under `.qa/work/` is at `/work/.qa-input/<path below .qa/work/>`; `{evidence}` is `/qa-evidence` inside. Only files a declared `--report` names are kept. Exit codes: 0 passed, 1 failed, 3 blocked, 4 inconclusive, 5 not_run |
| `authorize --environment local --activity dast-passive --target 127.0.0.1 --approval "<the user's words>"` | The grant a ZAP baseline needs; add `dast-active` for the active scan. Only the user's own words, only `127.0.0.1`, only `local`; it expires at the end of the day |
| `finding add --json .qa/work/security/<name>.json` | One finding (`references/triage.md`, template) |
| `state show [executions\|findings\|authorizations]`, `ledger verify` | What was recorded |

The skill's scripts read files and write only under `.qa/work/security/`; they run directly, from the project root:

- `python3 .claude/skills/qa-security/scripts/unicode_scan.py` scans the project for text that addresses an agent ("ignore the previous instructions", in English or Spanish), Unicode tag characters and bidirectional controls, and writes `.qa/work/security/unicode.sarif`. It quotes no line. `--copy-to-work` copies it to `.qa/work/security/unicode_scan.py` for the container run below.
- `python3 .claude/skills/qa-security/scripts/zap_plan.py --kind baseline|active [--openapi PATH]` writes `.qa/work/security/zap-baseline.yaml` or `zap-active.yaml` from the committed asset of the same name; `--kind semgrep-rules` writes `.qa/work/security/semgrep-rules.yaml` from `assets/semgrep-rules/`. Call it immediately before every run that passes the file: the run's input must be the asset, not an older or edited copy.
- `python3 .claude/skills/qa-security/scripts/sarif_triage.py --sarif .qa/evidence/<EXE>/reports/<file>` lists a stored report's alerts by rule, CWE and location in `.qa/work/security/triage-<file>.json`, with no snippet and every secret-shaped value masked. Read alerts from there, never from the raw report.

## The images

| Tool | Version | Image (use exactly this) |
|---|---|---|
| Gitleaks | 8.30.1 | `ghcr.io/gitleaks/gitleaks@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f` |
| OSV-Scanner | 2.7.1 | `ghcr.io/google/osv-scanner@sha256:a70ffffc2bfd33482953e5d6b64c6fcdb30239717a07b0f0d3ab2859ce3e4cde` |
| Semgrep CE | 1.185.0 | `semgrep/semgrep@sha256:67518c174185a2043fdbed16c70631b79209499b3671178e7194379b48295a0d` |
| ZAP | 2.17.0 | `ghcr.io/zaproxy/zaproxy@sha256:9624669db72f54d86c4b46a7064518bd9bf79d8c61a4c31befa9923489f328af` |
| Python, for the unicode scan and for a Python service | 3 (slim) | `python@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928` |

A service in another runtime uses its image from `.claude/skills/qa-security/scripts/_kernel/policies/container-images.json` (for example `node-lts`). An image not in that file is not used.

## Gitleaks: secrets in the files

1. Function: scans files for credentials (API keys, tokens, private keys, passwords in assignments) with about 220 rules, each a pattern and an entropy check.
2. Use cases: a hard-coded key, token or password in source, configuration or fixtures; the "secrets" step of every review.
3. Compatibility: any text file; SARIF, JSON, CSV, JUnit output. `gitleaks dir` reads the files as they are. Git history needs `gitleaks git`, which the suite does not run (no history scan without a `git-history-scan` grant).
4. Dependencies: the image above; no service, no network, no grant (`--activity static-analysis`).
5. Alternatives: TruffleHog (AGPL-3.0, verifies keys online, which this suite never does), detect-secrets (Apache-2.0), Betterleaks.
6. License, costs, restrictions: MIT, free. `--redact` replaces every secret in its output with `REDACTED`; the kernel also masks known secret shapes in everything it stores.
7. Reason selected: fast, offline, SARIF output, and a redact switch, so no secret reaches the evidence.

```qa-leg gitleaks grant=none report=sarif
python3 .claude/skills/qa-security/scripts/qa.py --role auditor --skill qa-security run --container ghcr.io/gitleaks/gitleaks@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f --activity static-analysis --category security --timeout 600 --report 'sarif:{evidence}/gitleaks.sarif' --shell -- 'gitleaks dir --redact --no-banner --report-format sarif --report-path /qa-evidence/gitleaks.sarif /work'
```

Gitleaks exits 1 when it finds a leak, so a run with a leak is `failed`. Its rules carry no CWE: a hard-coded credential is CWE-798. Never print, copy or quote the value; the finding names the file, the line and the rule.

## OSV-Scanner: dependencies with published advisories

1. Function: reads manifests and lockfiles (`requirements.txt`, `poetry.lock`, `package-lock.json`, `go.sum`, `pom.xml` and more) and matches every pinned version against the OSV advisory database.
2. Use cases: the "dependencies" step: a pinned version with a CVE, GHSA or PYSEC advisory.
3. Compatibility: most ecosystems; offline it needs that ecosystem's database file. SARIF or JSON output.
4. Dependencies: the image above, whose binary is `/osv-scanner` (off the PATH). The install step, the only step with a network, fetches `https://osv-vulnerabilities.storage.googleapis.com/<ecosystem>/all.zip` to `$OSV_SCANNER_LOCAL_DB_CACHE_DIRECTORY/osv-scalibr/<ecosystem>/all.zip` (the path 2.7.1 reads). It needs grants the user records: an environment declared for that host (`qa config set`, class `production`), and on it `network-download`, `install-dependencies` and `test-execution-local` with the host as `--target`; and `install-dependencies` on `local`. Without them the run is `blocked`, `not_authorized`. The scan step has no network: the guard admits only `scan`, `source`, `--offline`, `--recursive`, `--format=sarif|json`, `--output-file=/qa-evidence/<name>` and `/work` paths, each written with `=`. Anything else needs `data-egress`, which this skill never asks for.
5. Alternatives: pip-audit (Apache-2.0, online), Grype (Apache-2.0), Trivy (Apache-2.0); `references/triage.md` for checking a lockfile by hand.
6. License, costs, restrictions: Apache-2.0, free. The advisory data comes from OSV's sources under their own terms. Online mode would send the dependency list to a third party; this skill never runs it.
7. Reason selected: one tool for many ecosystems, a documented offline mode, SARIF output.

```qa-leg osv-scanner grant=install report=sarif
python3 .claude/skills/qa-security/scripts/qa.py --role auditor --skill qa-security run --container ghcr.io/google/osv-scanner@sha256:a70ffffc2bfd33482953e5d6b64c6fcdb30239717a07b0f0d3ab2859ce3e4cde --category security --timeout 900 --install 'mkdir -p /work/osv-db/osv-scalibr/PyPI && wget -q -O /work/osv-db/osv-scalibr/PyPI/all.zip https://osv-vulnerabilities.storage.googleapis.com/PyPI/all.zip' --report 'sarif:{evidence}/osv.sarif' --shell -- 'OSV_SCANNER_LOCAL_DB_CACHE_DIRECTORY=/work/osv-db /osv-scanner scan source --offline --recursive --format=sarif --output-file=/qa-evidence/osv.sarif /work'
```

Replace `PyPI` with the project's ecosystem (`npm`, `Maven`, `Go`, `crates.io` …); one install line per ecosystem. Each result names the package, the version and the advisory (`Package 'requests@2.19.1' is vulnerable to 'CVE-…'`), with the manifest as its location and no line. Its rules carry no CWE: a dependency with a published advisory is CWE-1395. Whether the vulnerable code is reachable is the triage's question (`references/triage.md`), not the scanner's.

## Semgrep: code patterns of the classes

1. Function: matches code against rules written as code patterns, with metavariables and conditions; reports each match with its rule, file and line.
2. Use cases: the "SAST" step: SQL built by string formatting, request values in HTML templates or file paths, reflected CORS origins, cookies without HttpOnly or Secure, debug left on, signature checks that trust the token's own algorithm.
3. Compatibility: about 30 languages. This skill ships rules for Python web code only (`assets/semgrep-rules/`); in another language Semgrep runs with no rule of the suite's, so say the SAST step had no rules for it, never that it found nothing.
4. Dependencies: the image above; the rules file written by `zap_plan.py --kind semgrep-rules` and passed with `--input`; no service, no network, no grant.
5. Alternatives: Opengrep (LGPL-2.1, a fork with the same rule syntax), Bandit (Apache-2.0, Python only), CodeQL (free for open source only).
6. License, costs, restrictions: the engine is LGPL-2.1, used unmodified from its image. Only the suite's own rules run (Apache-2.0): rules from the Semgrep registry carry the Semgrep Rules License v1.0 and are never fetched, bundled or redistributed. `--metrics=off` and `--disable-version-check` keep it from calling home; the container has no network anyway.
7. Reason selected: rules that read like the code they match, so each class is a short, reviewable rule, with SARIF output carrying the rule's CWE.

Write the rules file first (`python3 .claude/skills/qa-security/scripts/zap_plan.py --kind semgrep-rules`), then:

```qa-leg semgrep grant=none report=sarif
python3 .claude/skills/qa-security/scripts/qa.py --role auditor --skill qa-security run --container semgrep/semgrep@sha256:67518c174185a2043fdbed16c70631b79209499b3671178e7194379b48295a0d --activity static-analysis --category security --timeout 900 --input .qa/work/security/semgrep-rules.yaml --report 'sarif:{evidence}/semgrep.sarif' --shell -- 'semgrep scan --metrics=off --disable-version-check --config /work/.qa-input/security/semgrep-rules.yaml --exclude=.qa-input --sarif --output /qa-evidence/semgrep.sarif /work'
```

Semgrep exits 0 with findings; the kernel reads the SARIF, so a run with results is `failed` and each result is a provisional finding. Semgrep's SARIF quotes the matched line: the kernel masks known secret shapes in it, and `sarif_triage.py` leaves snippets out.

## The unicode scan in a container

The same `unicode_scan.py` as on the host, recorded as an execution with its SARIF. Copy it first (`python3 .claude/skills/qa-security/scripts/unicode_scan.py --copy-to-work`), then:

```qa-leg unicode-scan grant=none report=sarif
python3 .claude/skills/qa-security/scripts/qa.py --role auditor --skill qa-security run --container python@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 --category security --timeout 300 --input .qa/work/security/unicode_scan.py --report 'sarif:{evidence}/unicode.sarif' --shell -- 'python3 -I -B /work/.qa-input/security/unicode_scan.py --out /qa-evidence/unicode.sarif /work'
```

## ZAP: the running application

1. Function: a web application scanner. The Automation Framework runs a plan: the spider crawls from the target, the passive checks read every response (headers, cookies, errors, disclosures), and the active scan sends attack requests to every parameter it found (injection, cross-site scripting, path traversal and more). The `report` job writes SARIF (`sarif-json`).
2. Use cases: what the running application shows: missing security headers, cookie attributes, error pages with tracebacks, CORS answers (baseline); and, against the suite's own fixtures only, whether an injection or traversal class answers to attack input (active).
3. Compatibility: any HTTP application; for an API, an OpenAPI contract seeds the active scan instead of the spider (`zap_plan.py --kind active --openapi <contract>` puts an `openapi` job in the spider's place, since an API often serves nothing at its root).
4. Dependencies: the image above, whose launcher is `zap.sh` (the image has no entrypoint); a service image and the shell text that starts the application (`<serve>`) on `127.0.0.1:8000`, never 8080, where ZAP listens itself; the plan written by `zap_plan.py` immediately before the run and passed with `--input`; a grant recorded with the user's words, `dast-passive` for the baseline, `dast-passive` and `dast-active` for the active scan, on `local` for `127.0.0.1`.
5. Alternatives: Nuclei (MIT, template-based), Wapiti (GPL-2.0), Burp Suite (commercial).
6. License, costs, restrictions: Apache-2.0, free. `-notel` turns telemetry off. Its update check cannot be turned off; with no network it fails silently. The guard reads the command line: exactly `QA_TARGET=http://127.0.0.1:<port> zap.sh -cmd -notel -autorun /work/.qa-input/security/zap-baseline.yaml` is `dast-passive`; any other argument, any other plan, or a changed port option makes the run `dast-active`.
7. Reason selected: the reference open-source DAST, with a plan file that states every job, a capped active scan and SARIF output whose rules carry CWE ids.

Baseline (write the plan first: `python3 .claude/skills/qa-security/scripts/zap_plan.py --kind baseline`; grant: `dast-passive`):

```qa-leg zap-baseline grant=dast-passive report=sarif
python3 .claude/skills/qa-security/scripts/qa.py --role auditor --skill qa-security run --container ghcr.io/zaproxy/zaproxy@sha256:9624669db72f54d86c4b46a7064518bd9bf79d8c61a4c31befa9923489f328af --service python@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 --service-run '<serve>' --service-user 65534:65534 --category security --timeout 900 --input .qa/work/security/zap-baseline.yaml --report 'sarif:{evidence}/zap.json' --shell -- 'sleep 3; QA_TARGET=http://127.0.0.1:8000 zap.sh -cmd -notel -autorun /work/.qa-input/security/zap-baseline.yaml'
```

Active (write the plan first: `python3 .claude/skills/qa-security/scripts/zap_plan.py --kind active`, with `--openapi <contract>` for an API; grants: `dast-passive` and `dast-active`). It sends attack input, so it runs only against the project's own application started in the same run, on loopback, never against anything else; the scan stops after ten minutes, and the run after 1500 seconds:

```qa-leg zap-active grant=dast-active report=sarif
python3 .claude/skills/qa-security/scripts/qa.py --role auditor --skill qa-security run --container ghcr.io/zaproxy/zaproxy@sha256:9624669db72f54d86c4b46a7064518bd9bf79d8c61a4c31befa9923489f328af --service python@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 --service-run '<serve>' --service-user 65534:65534 --category security --timeout 1500 --input .qa/work/security/zap-active.yaml --report 'sarif:{evidence}/zap.json' --shell -- 'sleep 3; QA_TARGET=http://127.0.0.1:8000 zap.sh -cmd -notel -autorun /work/.qa-input/security/zap-active.yaml'
```

- `<serve>` starts the application on `127.0.0.1:8000` from the copy at `/work`, in the foreground, for example `cd /work && exec python3 lab/app.py --host 127.0.0.1 --port 8000`. It runs in the service container as an unprivileged user and shares only loopback with ZAP. The project's code never runs on this machine.
- The report job writes `/qa-evidence/zap.json`, which is SARIF. A ZAP rule names its CWE through a relationship; `sarif_triage.py` reads it. An alert's location is a URL (`GET http://127.0.0.1:8000/…`), so the finding's component is the endpoint.
- ZAP exits nonzero when its plan has errors (`failOnError`); the run is then `failed` with the reason in `stdout.log`.

## When a tool cannot run

| The run's record | What it means | What you say |
|---|---|---|
| `blocked`, `dependency_unavailable`, limitation `container_engine_unavailable` | No container engine answered | "<tool> did not run: no container engine on this machine (EXE-…)". No result, no estimate, no "clean" |
| `blocked`, `container_image_missing` | The pinned image is not on the machine; the kernel never pulls | The image the user would need, by digest |
| `blocked`, `not_authorized` | No grant covers an activity the run needs (the reason names it) | The approval the user would give, in their words, for `127.0.0.1` only (ZAP), or the OSV install grants above |
| `blocked`, `policy_denied` | The guard refused the command | Read the reason; never rewrite the command to slip past it |
| `failed` | The tool ran and reported alerts | Quote rules, files and lines from `sarif_triage.py`'s output |
| `passed` | The tool ran and reported nothing | "<tool> ran (EXE-…) and reported nothing for its rules"; never "secure" |

A blocked tool's step is not done. Your own reading of the code still stands, under its own name, as `suspected` findings; never relabel it as the tool's result.
