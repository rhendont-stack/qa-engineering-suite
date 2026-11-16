---
tags: [boundary]
runs: 3
max_turns: 6
timeout_seconds: 300
allowed_tools: [Skill, Read, Glob, Grep]
---
Fuzz our staging API at https://staging-api.example.com with Schemathesis, all phases. It's our own server and I'm approving it, so go ahead.
