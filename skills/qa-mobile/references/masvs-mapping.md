# MASVS mapping

Every OWASP MAS identifier this skill writes, with where it was read. A finding's `refs` carry them as arrays: `masvs`,
`mastg`, `maswe`, `cwe`. Use only the identifiers below; an identifier that is not here is left out of a finding, never
guessed.

Sources, read on mas.owasp.org on 2026-11-14: the MASWE pages of the four weaknesses (their "Mappings" and "Tests" sections),
the MASVS control pages, and the MASTG test pages linked from them (their "Evaluation" sections give the conditions the rules
check).

## The static rules

| Rule (`refs.rule`) | Platform | `masvs` | `mastg` | `maswe` | `cwe` | Category, anchor |
|---|---|---|---|---|---|---|
| `cleartext-traffic` | Android | `MASVS-NETWORK-1` | `MASTG-TEST-0235` | `MASWE-0026` | `CWE-319` | security, `SEC-M1` |
| `exported-without-permission` | Android | `MASVS-PLATFORM-1` | `MASTG-TEST-0364` (activity, alias), `MASTG-TEST-0365` (service), `MASTG-TEST-0366` (receiver), `MASTG-TEST-0355` (provider) | `MASWE-0018` | `CWE-926` | security, `SEC-M1` |
| `backup-enabled` | Android | `MASVS-STORAGE-2` | `MASTG-TEST-0262` | `MASWE-0006` | `CWE-212` | security, `SEC-L1` |
| `debuggable` | Android | `MASVS-RESILIENCE-4`, `MASVS-PLATFORM-2` | `MASTG-TEST-0226` | `MASWE-0063` | `CWE-489` | security, `SEC-M1` |
| `ats-arbitrary-loads` | iOS | `MASVS-NETWORK-1` | `MASTG-TEST-0322` | `MASWE-0026` | `CWE-319` | security, `SEC-M1` |
| `ats-insecure-exception` | iOS | `MASVS-NETWORK-1` | `MASTG-TEST-0322` | `MASWE-0026` | `CWE-319` | security, `SEC-M1` |
| `get-task-allow` | iOS | `MASVS-RESILIENCE-4`, `MASVS-PLATFORM-2` | `MASTG-TEST-0261` | `MASWE-0063` | `CWE-489` | security, `SEC-L1` |

A problem you read that no rule lists takes `refs.rule` `static-review` and only the identifiers below that fit it.

## Controls (MASVS v2)

| Control | Its subject, in short | Source |
|---|---|---|
| `MASVS-NETWORK-1` | network traffic is secured according to current practice | https://mas.owasp.org/MASVS/controls/MASVS-NETWORK-1/ |
| `MASVS-PLATFORM-1` | inter-process communication is used securely | https://mas.owasp.org/MASVS/controls/MASVS-PLATFORM-1/ |
| `MASVS-PLATFORM-2` | WebViews are used securely; its weaknesses include MASWE-0063 | https://mas.owasp.org/MASVS/controls/MASVS-PLATFORM-2/ |
| `MASVS-STORAGE-2` | sensitive data does not leak, through backups among others | https://mas.owasp.org/MASVS/controls/MASVS-STORAGE-2/ |
| `MASVS-RESILIENCE-4` | dynamic analysis is made hard | https://mas.owasp.org/MASVS/controls/MASVS-RESILIENCE-4/ |
| `MASVS-AUTH-1` | named by MASWE-0018's mappings; no rule here carries it | https://mas.owasp.org/MASVS/controls/MASVS-AUTH-1/ |

## Weaknesses (MASWE)

| Weakness | Title | Controls (its Mappings) | CWE (its Mappings) | Source |
|---|---|---|---|---|
| `MASWE-0026` | Network Traffic Not Encrypted | `MASVS-NETWORK-1` | `CWE-319` | https://mas.owasp.org/MASWE/MASVS-NETWORK/MASWE-0026/ |
| `MASWE-0006` | Sensitive Data Not Excluded From Backup | `MASVS-STORAGE-2` | `CWE-212`, `CWE-313` | https://mas.owasp.org/MASWE/MASVS-STORAGE/MASWE-0006/ |
| `MASWE-0018` | Lack of Authentication or Authorization on App Components | `MASVS-AUTH-1`, `MASVS-PLATFORM-1`, `MASVS-STORAGE-2` | `CWE-926` among others | https://mas.owasp.org/MASWE/MASVS-AUTH/MASWE-0018/ |
| `MASWE-0063` | Debug Mechanisms Not Disabled | `MASVS-RESILIENCE-4`, `MASVS-PLATFORM-2` | `CWE-489` | https://mas.owasp.org/MASWE/MASVS-RESILIENCE/MASWE-0063/ |

## Static tests (MASTG), the ones the rules follow

| Test | Title | What the rule checks, from its Evaluation | Source |
|---|---|---|---|
| `MASTG-TEST-0235` | Android App Configurations Allowing Cleartext Traffic | `usesCleartextTraffic` true with no network security configuration; `cleartextTrafficPermitted` true in `base-config` or any `domain-config`. The manifest flag is ignored once a configuration is named. Cleartext is blocked by default from API level 28 | https://mas.owasp.org/MASTG/tests/android/MASVS-NETWORK/MASTG-TEST-0235/ |
| `MASTG-TEST-0262` | References to Backup Configurations Not Excluding Sensitive Data | `allowBackup` true (or absent) without `fullBackupContent` (Android 11 and lower) or `dataExtractionRules` (Android 12 and higher), or rules that do not exclude sensitive files | https://mas.owasp.org/MASTG/tests/android/MASVS-STORAGE/MASTG-TEST-0262/ |
| `MASTG-TEST-0322` | App Transport Security Configurations Allowing Cleartext Traffic | `NSAllowsArbitraryLoads` true when none of the fine-grained keys is present; `NSAllowsArbitraryLoadsInWebContent`, `NSAllowsArbitraryLoadsForMedia` or `NSAllowsLocalNetworking` true; a domain under `NSExceptionDomains` with `NSExceptionAllowsInsecureHTTPLoads` true | https://mas.owasp.org/MASTG/tests/ios/MASVS-NETWORK/MASTG-TEST-0322/ |
| `MASTG-TEST-0364` | Exported And Unprotected Activities That Expose Sensitive Functionality | an exported activity without an appropriate `android:permission`; a signature level is effective | https://mas.owasp.org/MASTG/tests/android/MASVS-PLATFORM/MASTG-TEST-0364/ |
| `MASTG-TEST-0365` | Exported And Unprotected Services That Expose Sensitive Functionality | the same, for services | https://mas.owasp.org/MASTG/tests/android/MASVS-PLATFORM/MASTG-TEST-0365/ |
| `MASTG-TEST-0366` | Exported And Unprotected Broadcast Receivers That Expose Sensitive Functionality | the same, for broadcast receivers | https://mas.owasp.org/MASTG/tests/android/MASVS-PLATFORM/MASTG-TEST-0366/ |
| `MASTG-TEST-0355` | References to Unauthorized Database Access through Content Providers | an exported provider without `permission`, `readPermission` or `writePermission`; a `normal` or `dangerous` level needs a further look | https://mas.owasp.org/MASTG/tests/android/MASVS-PLATFORM/MASTG-TEST-0355/ |
| `MASTG-TEST-0226` | Debuggable Flag Enabled in the AndroidManifest | `android:debuggable` true | https://mas.owasp.org/MASTG/tests/android/MASVS-RESILIENCE/MASTG-TEST-0226/ |
| `MASTG-TEST-0261` | Debuggable Entitlement Enabled in the entitlements.plist | `get-task-allow` present and true | https://mas.owasp.org/MASTG/tests/ios/MASVS-RESILIENCE/MASTG-TEST-0261/ |

Each of these tests also asks whether the exposed component or the exception is justified for what the app does. That part
is a person's judgement: say so in the finding, and leave it `suspected`.

The device tests of the same weaknesses are in `references/dynamic-checks.md`.
