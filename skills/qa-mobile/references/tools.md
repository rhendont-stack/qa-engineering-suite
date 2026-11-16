# Tools

This skill installs nothing on this machine and downloads nothing: no Android SDK, platform tools, emulator image, Xcode,
simulator runtime, device driver or instrumentation tool. The static work needs only Python's standard library. Every tool
run goes through the kernel, in a container from an image pinned by digest, with no network. The kernel never pulls an
image. An engine that does not answer, an image that is not on the machine, or a missing grant makes the run `blocked`: that
record is the result. Never run a tool another way (a local install, `brew`, `sdkmanager`, `npx`, a binary found on the PATH,
a hosted scanner).

Every kernel call is written out in full, from the project root:

`python3 <skill-dir>/scripts/qa.py --role verifier --skill qa-mobile <command>`

## Kernel commands used

| Command | Use |
|---|---|
| `init`, `config show` | The workspace: the declared environments and the grants. A container run needs no trust: never change `project.trust` |
| `run --container IMAGE@DIGEST [--input .qa/work/mobile/FILE] --category security --timeout N [--report sarif:{evidence}/FILE] [--shell] -- COMMAND` | One run in a container. The project is copied to `/work` (`.qa/` stays out); an `--input` file is at `/work/.qa-input/mobile/<name>`; `{evidence}` is `/qa-evidence` inside. Only the files a declared `--report` names are kept. Exit codes: 0 passed, 1 failed, 3 blocked, 4 inconclusive, 5 not_run |
| `finding add --json .qa/work/mobile/<name>.json`, `finding update FND --json FILE`, `finding state FND duplicate --reason R --duplicate-of KEEP` | One finding, a change to one, one record of a problem marked as a duplicate of another (`references/triage.md`) |
| `ingest bundle FILE` | Imports an evidence bundle that `qa report bundle` made on another machine, with that machine's executions and results. Only for a file the user names |
| `ingest junit FILE --from EXE`, `ingest sarif FILE --from EXE` | Attaches a report file to a recorded execution here (for a device check, its blocked run), recorded as attested: what the file says, not a run here. Only for a file the user names. The kernel's other formats (`ctrf`, `lcov`, `cobertura`, `jacoco`, `mutation`, `k6`, `axe`, `lighthouse`) do not carry a device check |
| `state show [executions\|findings\|authorizations]`, `ledger verify` | What was recorded |

There is no grant to record in this skill. `authorize` is never called: not for `device-control`, which a device or emulator
run needs and this machine has no use for, not for `install-dependencies`, not for `network-download`.

The skill's scripts read files and write only under `.qa/work/mobile/`; they run directly, from the project root:

- `python3 <skill-dir>/scripts/mobile_static.py --root . --out .qa/work/mobile/mobile-static.sarif` reads the
  Android manifests, network security configurations and `build.gradle` SDK levels, and the iOS `Info.plist` and
  entitlements files, and lists every setting its seven rules name, with `path:line` and its MASVS, MASTG, MASWE and CWE
  references (`references/masvs-mapping.md`). `--format json` writes its own JSON instead of SARIF. `--copy-to-work` copies
  the script to `.qa/work/mobile/mobile_static.py` for the container leg.
- `python3 <skill-dir>/scripts/dynamic_checks.py [--blocked-execution EXE]` writes
  `.qa/work/mobile/dynamic-checks.json`: the six device checks of `references/dynamic-checks.md`, each `not_run`, or
  `blocked` with the EXE of the blocked device run, and each with its remediation. It has no option for any other status.

## The image

| Tool | Version | Image (use exactly this) |
|---|---|---|
| Python, for the static check | 3 (slim) | `python@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928` |

No image of `<skill-dir>/scripts/_kernel/policies/container-images.json` holds a device tool, an emulator or a
simulator, and none is added: a device check runs on a machine that has a device, not here.

## The static check

1. Function: reads the configuration files a mobile build reads, under a directory, with Python's `xml.parsers.expat` and
   `plistlib`, and writes SARIF 2.1.0 (or JSON) whose results carry `masvs`, `mastg`, `maswe` and `cwe` references and an
   `approximate` flag. Rules: `cleartext-traffic`, `exported-without-permission` (the launcher activity is not listed),
   `backup-enabled`, `debuggable`, `ats-arbitrary-loads`, `ats-insecure-exception`, `get-task-allow`.
2. Use cases: the first step of every mobile review: what the manifest, the network security configuration, the App
   Transport Security settings and the entitlements allow, with the file and line a person can open.
3. Compatibility: Android projects with `AndroidManifest.xml` and `res/xml` files; iOS projects with `Info.plist` and
   `.entitlements` files (XML or binary property lists; a binary list has no lines, so its results cite line 1). The SDK
   levels are read from `build.gradle` or `build.gradle.kts` as text, so variables, flavours and build types are not
   resolved: a result that rests on them is `approximate`. The manifest merger, the signed entitlements and the app's code
   are not seen: a result is a lead for a person or a device check, never a verdict on the shipped app.
4. Dependencies: Python 3.9 or newer, standard library only; nothing else. In the container leg, the Python image above and
   the copy written by `--copy-to-work`.
5. Alternatives: MobSF (GPL-3.0), which reads the same settings from a built package; a person's reading with the MASTG
   static tests (`references/masvs-mapping.md`); the MASTG device tests on a device (`references/dynamic-checks.md`).
6. License, costs, restrictions: part of this skill (Apache-2.0); no network. Files over 1 MiB, links, paths that resolve
   outside the root, files that are not well-formed, and XML with any DOCTYPE other than Apple's public plist one (without an
   internal subset), an entity declaration or an undeclared entity reference are listed `unreadable` with the reason, and
   nothing is reported from them.
7. Reason selected: it runs everywhere, needs no build, no SDK and no device, and cites the file and line.

```qa-leg mobile-static grant=none report=sarif
python3 <skill-dir>/scripts/qa.py --role verifier --skill qa-mobile run --container python@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 --category security --timeout 300 --input .qa/work/mobile/mobile_static.py --report 'sarif:{evidence}/mobile-static.sarif' --shell -- 'cd /work && python3 -I -B /work/.qa-input/mobile/mobile_static.py --root . --out /qa-evidence/mobile-static.sarif'
```

The kernel reads the SARIF: a run with results is `failed`, and each result opens a provisional finding of the run's
category, `security`, unless a finding with the same rule, file and category is already recorded. So record the host
check's findings with `finding add` first, each with `refs.rule` the rule id, `component.path` and `refs.location` the file,
and no `key_attributes` (`references/triage.md`): the leg's result for the same rule in the same file is then a new
sighting of that finding, not a second record.

## The device route

A device check (`references/dynamic-checks.md`) installs the app on a device, an emulator or a simulator and drives it. The
guard's `device-control` rule covers the programs that do that (`adb`, `emulator`, `xcrun`, `simctl`, `maestro`, `appium`,
`detox`), and a run of one needs a `device-control` grant, which this skill never records. The leg below records that the
route is closed here: the guard blocks it before anything starts, and nothing is installed or downloaded.

```qa-leg device-check grant=device-control report=none
python3 <skill-dir>/scripts/qa.py --role verifier --skill qa-mobile run --container python@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 --category security --timeout 60 -- adb devices
```

Its record is `blocked`, reason `not_authorized`, naming `device-control`. Give its EXE to
`dynamic_checks.py --blocked-execution EXE`. A device program is never run any other way: not from Bash, not through a
container image that holds one, not with a grant recorded for the occasion.

## When a tool cannot run

| The run's record | What it means | What you say |
|---|---|---|
| `blocked`, `not_authorized`, naming `device-control` | The device route: no grant, by design | "The device checks did not run: they need a device or emulator and a device-control grant (EXE-…)". No result, no estimate |
| `blocked`, `dependency_unavailable`, limitation `container_engine_unavailable` | No container engine answered | "The static leg did not run in a container: no container engine on this machine (EXE-…)". The host check's results stand as `suspected` findings |
| `blocked`, `container_image_missing` | The pinned image is not on the machine; the kernel never pulls | The image the user would need, by digest |
| `blocked`, `policy_denied` | The guard refused the command | Read the reason; never rewrite the command to slip past it |
| `failed` | The static leg ran and listed results | Its results are sightings of your findings, with the EXE |
| `passed` | The static leg ran and listed nothing | "The static leg ran (EXE-…) and listed nothing for its seven rules"; never "secure" |

A blocked run's step is not done. The static check's results and your own reading still stand, under their own names, as
`suspected` findings; never relabel them as a device check's result.
