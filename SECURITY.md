# Security policy

## Reporting a vulnerability

Report vulnerabilities privately through GitHub: open the **Security** tab of this repository and choose **Report a vulnerability**. Please do not open a public issue.

Include the affected file or command, the steps to reproduce the problem, and what an attacker could achieve. Handling is best effort, and there is no guaranteed response time.

Only test against systems you own or are authorized to test.

## Supported versions

No version has been released. Only the `main` branch receives fixes.

## Scope

**In scope:** the code and policies in this repository, in particular:

- the command guard and the authorization checks
- the integrity of the ledger and of recorded evidence
- secret redaction
- report rendering and claim checking
- anything that makes the kernel report a result that did not happen

**Out of scope:** vulnerabilities in third-party tools the suite invokes, such as test runners and scanners. Please report those to their own projects. A way in which this suite makes such a vulnerability easier to reach is in scope.

## Known limits

The command guard is defense in depth, not a security boundary. The classes of commands it does not cover are listed under "Limitations" in the [milestone 1 report](docs/reports/milestone-1.md). A case inside one of those classes is a known limit. A way around a class the guard claims to cover is a vulnerability, so please report it privately.
