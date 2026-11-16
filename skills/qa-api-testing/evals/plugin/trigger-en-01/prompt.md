---
tags: [trigger, en]
runs: 3
max_turns: 3
timeout_seconds: 180
allowed_tools: [Skill, Read, Glob, Grep]
---
Check that our orders API actually matches its OpenAPI spec: every documented endpoint, plus any route the code serves that the spec doesn't mention.
