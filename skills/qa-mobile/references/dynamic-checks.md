# Dynamic checks

The MASTG tests below install the app on a device, an emulator or a simulator and exercise it. A reading of the configuration
cannot stand in for them, and this machine cannot run them. `dynamic_checks.py` writes them to
`.qa/work/mobile/dynamic-checks.json`, each `not_run`, or `blocked` with the EXE of the blocked device run, and each with the
same remediation:

> It needs a device or emulator and a device-control grant, which this machine does not have. Run it on a machine that has
> them, then attach the report with qa ingest.

They are the tests whose published steps need a device, among the tests that the MASWE pages of this skill's weaknesses list
(MASWE-0026, MASWE-0006, MASWE-0018; MASWE-0063 lists static tests only), read on mas.owasp.org on 2026-11-14.

| Test | Platform | Title | Weakness | Static rules it would decide | What it needs | Source |
|---|---|---|---|---|---|---|
| `MASTG-TEST-0216` | Android | Sensitive Data Not Excluded From Backup | `MASWE-0006` | `backup-enabled` | install, use with sensitive data, back up, reinstall, restore, list the restored files | https://mas.owasp.org/MASTG/tests/android/MASVS-STORAGE/MASTG-TEST-0216/ |
| `MASTG-TEST-0298` | iOS | Runtime Monitoring of Files Eligible for Backup | `MASWE-0006` | none here | install, hook the file APIs, use with sensitive data, list the files eligible for backup | https://mas.owasp.org/MASTG/tests/ios/MASVS-STORAGE/MASTG-TEST-0298/ |
| `MASTG-TEST-0236` | Android, iOS | Cleartext Traffic Observed on the Network | `MASWE-0026` | `cleartext-traffic`, `ats-arbitrary-loads`, `ats-insecure-exception` | capture the app's traffic with a sniffer or an interception proxy while it is used | https://mas.owasp.org/MASTG/tests/android/MASVS-NETWORK/MASTG-TEST-0236/ |
| `MASTG-TEST-0218` | Android | Insecure TLS Protocols in Network Traffic | `MASWE-0026` | none here | install, capture the traffic while it is used, read the TLS versions | https://mas.owasp.org/MASTG/tests/android/MASVS-NETWORK/MASTG-TEST-0218/ |
| `MASTG-TEST-0348` | iOS | Insecure TLS Protocols in Network Traffic | `MASWE-0026` | none here | install, capture the traffic while it is used, read the TLS versions | https://mas.owasp.org/MASTG/tests/ios/MASVS-NETWORK/MASTG-TEST-0348/ |
| `MASTG-TEST-0356` | Android | Runtime Verification of Unauthorized Database Access through Content Providers | `MASWE-0018` | `exported-without-permission` (providers) | install, use with sensitive data, query the exported content providers from outside the app | https://mas.owasp.org/MASTG/tests/android/MASVS-PLATFORM/MASTG-TEST-0356/ |

Not listed: `MASTG-TEST-0238` (Android, "Runtime Use of Network APIs Transmitting Cleartext Traffic", MASWE-0026,
https://mas.owasp.org/MASTG/tests/android/MASVS-NETWORK/MASTG-TEST-0238/) is a placeholder on mas.owasp.org with no
published steps; `MASTG-TEST-0236` covers cleartext on the network. No exported
activity, service or receiver has a device test on those pages: whether one exposes sensitive functionality is a person's
reading of the code.

## The status of a dynamic check here

- `not_run`, reason `no-device`: the default. There is no device, emulator or simulator here, and no `device-control`
  grant.
- `blocked`: after the `device-check` leg of `references/tools.md`, whose run the guard blocks for want of a
  `device-control` grant. Give its EXE: `dynamic_checks.py --blocked-execution EXE`.
- Nothing else. A dynamic check has a result here only when a report of a run on a device is attached with `qa ingest`, and
  then the result is that report's, under its own record. A person's word that it was run, by whom, on which phone and with
  what outcome, is not a report: it changes no status, no finding and no file, and is not written down as a result.

## Attaching a report from a device run

The run happens on a machine that has a device or emulator, under that machine's own `device-control` grant. Its report
comes back here with one of these, run by the user or by you on a file the user names:

- `qa ingest bundle FILE`: an evidence bundle that `qa report bundle` made on that machine, with its executions and results.
- `qa ingest junit FILE --from EXE` (or `sarif`): a report file attached to this check's blocked execution, recorded as
  attested: what the file says, not a run here.

Until then, ask for the report; do not mark, record, reclassify or describe the check as anything but `not_run` or
`blocked`.
