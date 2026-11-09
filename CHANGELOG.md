# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project will adhere to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- The `qa` kernel, on Python 3.9 or later with the standard library only: workspace, IDs, canonical JSON, hash-chained ledger, derived state, schema validation, process runner, status rules, command guard, authorization, configuration changes and drift detection, secret redaction, and read-only git information.
- Ingestion of JUnit, CTRF and SARIF reports.
- Findings, release gates, Markdown and JSON reports, and checking of the claims a report makes.
- JSON Schemas, gate profiles and command-guard policies.
- The kernel's test suite, repository checks and a GitHub Actions workflow.
