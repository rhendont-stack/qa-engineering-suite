# OWASP and CWE references

Every security finding carries at least one CWE id (`refs.cwe`) and one OWASP Top 10 2025 category (`refs.owasp_top10_2025`). An API finding adds the API Security Top 10 2023 (`refs.owasp_api_2023`); an ASVS 5.0 chapter (`refs.asvs_5_0`) is optional. The CWE names the weakness; the OWASP category groups it for the reader.

**The assignments below are this suite's**, made from each category's description. OWASP publishes its own list of CWEs per category; where a CWE here is not on that list, the category is still the closest description, and the finding names the CWE either way. When in doubt, the CWE is the fact and the category is the label.

## OWASP Top 10 2025

| Code | Category |
|---|---|
| A01:2025 | Broken Access Control |
| A02:2025 | Security Misconfiguration |
| A03:2025 | Software Supply Chain Failures |
| A04:2025 | Cryptographic Failures |
| A05:2025 | Injection |
| A06:2025 | Insecure Design |
| A07:2025 | Authentication Failures |
| A08:2025 | Software or Data Integrity Failures |
| A09:2025 | Security Logging and Alerting Failures |
| A10:2025 | Mishandling of Exceptional Conditions |

## The classes this skill looks for

| CWE | Weakness | Top 10 2025 | API 2023 | ASVS 5.0 chapter | Found by |
|---|---|---|---|---|---|
| CWE-89 | SQL built from input | A05 | API8 when an API | V1 Encoding and Sanitization | reading, Semgrep `qa-sql-built-by-string-formatting`, ZAP active |
| CWE-79 | Input placed in a page as markup | A05 | — | V1 Encoding and Sanitization; V3 Web Frontend Security | reading, Semgrep `qa-request-value-in-html-template`, ZAP active |
| CWE-22 | File path from input not confined | A01 | API8 when an API | V5 File Handling | reading, Semgrep `qa-path-joined-with-a-request-value`, ZAP active |
| CWE-798 | Hard-coded credential | A07 | API2 when it signs API tokens | V13 Configuration; V11 Cryptography | Gitleaks |
| CWE-1395 | Dependency with a known vulnerability | A03 | API10 when it handles responses of another API | V15 Secure Coding and Architecture | OSV-Scanner |
| CWE-489 | Active debug code (tracebacks in responses) | A02 | API8 | V13 Configuration; V16 Security Logging and Error Handling | reading, Semgrep `qa-debug-enabled-in-settings`, ZAP passive |
| CWE-942 | Cross-origin policy allows any origin | A02 | API8 | V3 Web Frontend Security | reading, Semgrep `qa-cors-origin-reflected`, ZAP passive |
| CWE-1004 | Session cookie without HttpOnly | A02 | — | V3 Web Frontend Security; V7 Session Management | reading, Semgrep `qa-cookie-without-httponly`, ZAP passive |
| CWE-614 | Session cookie without Secure | A02 | — | V3 Web Frontend Security; V7 Session Management | reading, Semgrep `qa-cookie-without-secure`, ZAP passive |
| CWE-347 | Signature not verified (the token picks its own algorithm) | A04 | API2 | V9 Self-contained Tokens | reading, Semgrep `qa-signature-check-depends-on-token-algorithm` |
| CWE-693 | Missing protection headers (CSP, X-Content-Type-Options, X-Frame-Options) | A02 | API8 | V3 Web Frontend Security | reading, ZAP passive |
| CWE-1021 | Page can be framed (no frame-ancestors, no X-Frame-Options) | A02 | — | V3 Web Frontend Security | ZAP passive |
| CWE-1275 | Cookie without a SameSite attribute | A02 | — | V3 Web Frontend Security | ZAP passive |
| CWE-1427 | Text that addresses an agent, hidden characters (prompt injection planted in files) | — (OWASP Top 10 for LLM Applications 2025: LLM01 Prompt Injection) | — | — | `unicode_scan.py` |
| CWE-284, CWE-639 | Object reached without an ownership check | A01 | API1 Broken Object Level Authorization | V8 Authorization | reading, an authorization test |
| CWE-285 | Function reached without a role check | A01 | API5 Broken Function Level Authorization | V8 Authorization | reading, an authorization test |
| CWE-915 | Fields written or read beyond the caller's rights | A01 | API3 Broken Object Property Level Authorization | V8 Authorization; V15 | reading |
| CWE-770 | No limit on size, rate or count | A06 | API4 Unrestricted Resource Consumption | V2 Validation and Business Logic | reading |
| CWE-918 | Server fetches a URL from input | A01 | API7 Server Side Request Forgery | V1; V12 Secure Communication | reading |
| CWE-307 | Authentication without attempt limits | A07 | API2 Broken Authentication | V6 Authentication | reading |
| CWE-778, CWE-532 | Security events not logged; secrets written to logs | A09 | — | V16 Security Logging and Error Handling | reading |
| CWE-755 | Error path fails open | A10 | — | V16 | reading |

`refs.owasp_top10_2025` holds the code in its full form, `A05:2025`; `refs.owasp_api_2023` holds `API1:2023`; `refs.asvs_5_0` holds the chapter, `V1`. ASVS is cited at chapter level only: a requirement number is cited only when you read that requirement in the ASVS 5.0 text itself.

## OWASP API Security Top 10 2023

| Code | Risk |
|---|---|
| API1:2023 | Broken Object Level Authorization |
| API2:2023 | Broken Authentication |
| API3:2023 | Broken Object Property Level Authorization |
| API4:2023 | Unrestricted Resource Consumption |
| API5:2023 | Broken Function Level Authorization |
| API6:2023 | Unrestricted Access to Sensitive Business Flows |
| API7:2023 | Server Side Request Forgery |
| API8:2023 | Security Misconfiguration |
| API9:2023 | Improper Inventory Management |
| API10:2023 | Unsafe Consumption of APIs |

## ASVS 5.0 chapters

V1 Encoding and Sanitization; V2 Validation and Business Logic; V3 Web Frontend Security; V4 API and Web Service; V5 File Handling; V6 Authentication; V7 Session Management; V8 Authorization; V9 Self-contained Tokens; V10 OAuth and OIDC; V11 Cryptography; V12 Secure Communication; V13 Configuration; V14 Data Protection; V15 Secure Coding and Architecture; V16 Security Logging and Error Handling; V17 WebRTC.

## Adversarial authorization testing

Authorization (API1, API3, API5) is tested with two identities the project itself provides, against the project's own application started in a container on `127.0.0.1`: identity A's request for an object of identity B, a low-role identity's request for an administrative function, a write that names a field the role may not set. Each is a request with a documented, benign value and a recorded expectation (403 or 404), not an attack string, and it runs under the grants the run records (`dast-passive` reads, `dast-active` when it writes). Without two identities in the project, say so and leave the class as `suspected` from the reading.
